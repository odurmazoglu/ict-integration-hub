"""Purpose-built read-only account.move header verification."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.application.workbench.vendor_bill_readback import VendorBillHeaderVerification
from app.erp.exceptions import ErpRepositoryResponseError
from app.erp.odoo.adapter import OdooReadOnlyAdapter

VENDOR_BILL_HEADER_VERIFICATION_FIELDS = (
    "id",
    "company_id",
    "state",
    "move_type",
    "partner_id",
    "currency_id",
    "amount_untaxed",
    "amount_tax",
    "amount_total",
)
SAFE_VENDOR_BILL_HEADER_ERROR = "Odoo Vendor Bill header verification read returned an unsafe response."


class OdooVendorBillHeaderVerificationReader:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def read_vendor_bill(self, *, move_id: int, company_id: int) -> VendorBillHeaderVerification | None:
        if not _positive_int(move_id) or not _positive_int(company_id):
            raise ErpRepositoryResponseError(SAFE_VENDOR_BILL_HEADER_ERROR)
        records = self._adapter.search_read(
            model="account.move",
            domain=[["id", "=", move_id], ["company_id", "=", company_id], ["move_type", "=", "in_invoice"]],
            fields=list(VENDOR_BILL_HEADER_VERIFICATION_FIELDS),
            limit=2,
        )
        if not records:
            return None
        if len(records) != 1:
            raise ErpRepositoryResponseError(SAFE_VENDOR_BILL_HEADER_ERROR)
        record = records[0]
        record_id = _required_positive_int(record.get("id"))
        record_company_id = _required_many2one_id(record.get("company_id"))
        move_type = _required_text(record.get("move_type"))
        if record_id != move_id or record_company_id != company_id or move_type != "in_invoice":
            raise ErpRepositoryResponseError(SAFE_VENDOR_BILL_HEADER_ERROR)
        return VendorBillHeaderVerification(
            move_id=record_id,
            company_id=record_company_id,
            state=_required_text(record.get("state")),
            move_type=move_type,
            partner_id=_required_many2one_id(record.get("partner_id")),
            currency=_required_many2one_name(record.get("currency_id")),
            amount_untaxed=_decimal(record.get("amount_untaxed")),
            amount_tax=_decimal(record.get("amount_tax")),
            amount_total=_decimal(record.get("amount_total")),
        )


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _required_positive_int(value: object) -> int:
    if not _positive_int(value):
        raise ErpRepositoryResponseError(SAFE_VENDOR_BILL_HEADER_ERROR)
    return value


def _required_many2one_id(value: object) -> int:
    if isinstance(value, (list, tuple)) and len(value) == 2 and _positive_int(value[0]):
        return value[0]
    if _positive_int(value):
        return value
    raise ErpRepositoryResponseError(SAFE_VENDOR_BILL_HEADER_ERROR)


def _required_many2one_name(value: object) -> str:
    if isinstance(value, (list, tuple)) and len(value) == 2 and _positive_int(value[0]):
        return _required_text(value[1])
    raise ErpRepositoryResponseError(SAFE_VENDOR_BILL_HEADER_ERROR)


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ErpRepositoryResponseError(SAFE_VENDOR_BILL_HEADER_ERROR)
    return value.strip()


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ErpRepositoryResponseError(SAFE_VENDOR_BILL_HEADER_ERROR)
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ErpRepositoryResponseError(SAFE_VENDOR_BILL_HEADER_ERROR) from exc
    if not parsed.is_finite():
        raise ErpRepositoryResponseError(SAFE_VENDOR_BILL_HEADER_ERROR)
    return parsed
