"""P0-PROD-12C: read-only account.move.line verification for an already-created Odoo Vendor Bill.

Closes the read-back verification caveat left open by P0-PROD-10E/10F/10G: at the time of
that pilot, ``account.move.line`` was not in ``OdooJson2Client.READ_ONLY_MODELS``, so the
exact line-level product_id/product_uom_id/quantity/price_unit/tax_ids Odoo actually persisted
could only be inferred from the write-time payload, never independently re-read. This module
adds exactly one narrow, purpose-specific reader for that -- not a generic Odoo model/domain
browsing capability. It has no path to any Odoo write: it is built on
:class:`OdooReadOnlyAdapter`, which exposes only ``search_read``/``search_read_all`` and
structurally cannot create, write, unlink, post, pay, or reconcile anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.application.workbench.vendor_bill_readback import VendorBillLineVerification
from app.erp.exceptions import ErpRepositoryResponseError
from app.erp.odoo.adapter import OdooReadOnlyAdapter

SAFE_ACCOUNT_MOVE_LINE_VERIFICATION_ERROR = "Odoo account.move.line verification read returned an unsafe response."

#: Fixed, minimal field list -- deliberately not caller-configurable. Extending this list is
#: a code change to this module, never a runtime/caller decision.
ACCOUNT_MOVE_LINE_VERIFICATION_FIELDS = (
    "id",
    "move_id",
    "product_id",
    "product_uom_id",
    "quantity",
    "price_unit",
    "tax_ids",
    "account_id",
)

VENDOR_BILL_INVOICE_LINE_VERIFICATION_FIELDS = (
    "id",
    "move_id",
    "product_id",
    "quantity",
    "price_unit",
    "tax_ids",
    "account_id",
    "price_subtotal",
    "price_total",
)


@dataclass(frozen=True, slots=True)
class AccountMoveLineVerification:
    """One independently-verified, normalized ``account.move.line`` record."""

    id: int
    move_id: int
    product_id: int | None
    product_uom_id: int | None
    quantity: Decimal
    price_unit: Decimal
    tax_ids: tuple[int, ...]
    account_id: int | None


class OdooAccountMoveLineVerificationReader:
    """Structurally read-only ``account.move.line`` reader, scoped to one ``move_id``.

    The search domain is always exactly ``[["move_id", "=", move_id]]`` -- there is no
    parameter, method, or code path that accepts a caller-supplied domain. This is not a
    generic Odoo browsing API; it exists solely to independently re-read the lines of one
    already-created Vendor Bill for verification.
    """

    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def read_lines_for_move(self, *, move_id: int) -> tuple[AccountMoveLineVerification, ...]:
        if type(move_id) is not int or isinstance(move_id, bool) or move_id <= 0:
            raise ErpRepositoryResponseError(SAFE_ACCOUNT_MOVE_LINE_VERIFICATION_ERROR)
        records = self._adapter.search_read(
            model="account.move.line",
            domain=[["move_id", "=", move_id]],
            fields=list(ACCOUNT_MOVE_LINE_VERIFICATION_FIELDS),
        )
        return tuple(_line_from_record(record, expected_move_id=move_id) for record in records)

    def read_invoice_lines_for_move(self, *, move_id: int) -> tuple[VendorBillLineVerification, ...]:
        """Read only invoice/product lines, excluding tax and payment-term journal items."""
        if type(move_id) is not int or isinstance(move_id, bool) or move_id <= 0:
            raise ErpRepositoryResponseError(SAFE_ACCOUNT_MOVE_LINE_VERIFICATION_ERROR)
        records = self._adapter.search_read(
            model="account.move.line",
            domain=[["move_id", "=", move_id], ["display_type", "=", "product"]],
            fields=list(VENDOR_BILL_INVOICE_LINE_VERIFICATION_FIELDS),
        )
        return tuple(_invoice_line_from_record(record, expected_move_id=move_id) for record in records)


def _invoice_line_from_record(record: object, *, expected_move_id: int) -> VendorBillLineVerification:
    if not isinstance(record, dict):
        raise ErpRepositoryResponseError(SAFE_ACCOUNT_MOVE_LINE_VERIFICATION_ERROR)
    line_id = _required_positive_int(record.get("id"))
    move_id = _required_many2one_id(record.get("move_id"))
    if move_id != expected_move_id:
        raise ErpRepositoryResponseError(SAFE_ACCOUNT_MOVE_LINE_VERIFICATION_ERROR)
    return VendorBillLineVerification(
        line_id=line_id,
        move_id=move_id,
        account_id=_optional_many2one_id(record.get("account_id")),
        product_id=_optional_many2one_id(record.get("product_id")),
        quantity=_decimal(record.get("quantity")),
        price_unit=_decimal(record.get("price_unit")),
        tax_ids=_tax_ids(record.get("tax_ids")),
        price_subtotal=_decimal(record.get("price_subtotal")),
        price_total=_decimal(record.get("price_total")),
    )


def _line_from_record(record: object, *, expected_move_id: int) -> AccountMoveLineVerification:
    if not isinstance(record, dict):
        raise ErpRepositoryResponseError(SAFE_ACCOUNT_MOVE_LINE_VERIFICATION_ERROR)

    line_id = _required_positive_int(record.get("id"))
    move_id = _required_many2one_id(record.get("move_id"))
    if move_id != expected_move_id:
        # A returned line belonging to a different move must never be silently accepted --
        # this is the one invariant this reader exists to protect, independent of whatever
        # domain Odoo itself claims to have applied.
        raise ErpRepositoryResponseError(SAFE_ACCOUNT_MOVE_LINE_VERIFICATION_ERROR)

    return AccountMoveLineVerification(
        id=line_id,
        move_id=move_id,
        product_id=_optional_many2one_id(record.get("product_id")),
        product_uom_id=_optional_many2one_id(record.get("product_uom_id")),
        quantity=_decimal(record.get("quantity")),
        price_unit=_decimal(record.get("price_unit")),
        tax_ids=_tax_ids(record.get("tax_ids")),
        account_id=_optional_many2one_id(record.get("account_id")),
    )


def _required_positive_int(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ErpRepositoryResponseError(SAFE_ACCOUNT_MOVE_LINE_VERIFICATION_ERROR)
    return value


def _required_many2one_id(value: object) -> int:
    resolved = _many2one_id(value)
    if resolved is None:
        raise ErpRepositoryResponseError(SAFE_ACCOUNT_MOVE_LINE_VERIFICATION_ERROR)
    return resolved


def _optional_many2one_id(value: object) -> int | None:
    # Odoo's own json/2 search_read represents an empty many2one as the literal `False` --
    # that is the one falsy shape treated as legitimate absence (e.g. product_id on an
    # account-only line). Any other unrecognized shape, including `True`, fails closed
    # rather than being silently treated as "absent".
    if value is False:
        return None
    resolved = _many2one_id(value)
    if resolved is None:
        raise ErpRepositoryResponseError(SAFE_ACCOUNT_MOVE_LINE_VERIFICATION_ERROR)
    return resolved


def _many2one_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, list | tuple) and len(value) >= 1:
        first = value[0]
        if isinstance(first, int) and not isinstance(first, bool) and first > 0:
            return first
        return None
    return None


def _tax_ids(value: object) -> tuple[int, ...]:
    if value is False:
        return ()
    if not isinstance(value, list | tuple):
        raise ErpRepositoryResponseError(SAFE_ACCOUNT_MOVE_LINE_VERIFICATION_ERROR)
    tax_ids: list[int] = []
    for item in value:
        if type(item) is not int or isinstance(item, bool) or item <= 0:
            raise ErpRepositoryResponseError(SAFE_ACCOUNT_MOVE_LINE_VERIFICATION_ERROR)
        tax_ids.append(item)
    return tuple(tax_ids)


def _decimal(value: object) -> Decimal:
    if type(value) is bool:
        raise ErpRepositoryResponseError(SAFE_ACCOUNT_MOVE_LINE_VERIFICATION_ERROR)
    if not isinstance(value, int | float | str | Decimal):
        raise ErpRepositoryResponseError(SAFE_ACCOUNT_MOVE_LINE_VERIFICATION_ERROR)
    try:
        decimal_value = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ErpRepositoryResponseError(SAFE_ACCOUNT_MOVE_LINE_VERIFICATION_ERROR) from exc
    if not decimal_value.is_finite():
        raise ErpRepositoryResponseError(SAFE_ACCOUNT_MOVE_LINE_VERIFICATION_ERROR)
    return decimal_value
