"""Structurally read-only read of a draft Vendor Bill's company-currency amounts (P1).

Reads the bill's ``invoice_currency_rate`` and every journal item's ``display_type``,
``amount_currency`` and ``balance``, so the Hub can prove the bill was created with the
invoice's rate and that Odoo recomputed the TRY lines, taxes and payable from it. Only
``search_read`` through :class:`OdooReadOnlyAdapter`; nothing is written or corrected.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.billing.fx_rate import VendorBillCompanyAmounts, VendorBillCompanyLine
from app.erp.exceptions import ErpRepositoryError, ErpRepositoryResponseError, ErpRepositoryTimeoutError
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.write.exceptions import VendorBillWriteTransportError, VendorBillWriteUnexpectedErpError

SAFE_COMPANY_AMOUNTS_ERROR = "Odoo Vendor Bill company-currency read returned an unsafe response."
MAX_BILL_LINES = 500


class OdooVendorBillCompanyAmountsReader:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def read_vendor_bill_company_amounts(self, *, move_id: int, company_id: int) -> VendorBillCompanyAmounts | None:
        try:
            return self._read(move_id=move_id, company_id=company_id)
        except ErpRepositoryTimeoutError as exc:
            raise VendorBillWriteTransportError(exc.safe_message) from exc
        except ErpRepositoryError as exc:
            raise VendorBillWriteUnexpectedErpError(exc.safe_message) from exc

    def _read(self, *, move_id: int, company_id: int) -> VendorBillCompanyAmounts | None:
        if not _positive_int(move_id) or not _positive_int(company_id):
            raise ErpRepositoryResponseError(SAFE_COMPANY_AMOUNTS_ERROR)
        moves = self._adapter.search_read(
            model="account.move",
            domain=[["id", "=", move_id], ["company_id", "=", company_id], ["move_type", "=", "in_invoice"]],
            fields=["id", "state", "currency_id", "invoice_currency_rate"],
            limit=2,
        )
        if not moves:
            return None
        if len(moves) != 1 or moves[0].get("id") != move_id:
            raise ErpRepositoryResponseError(SAFE_COMPANY_AMOUNTS_ERROR)
        move = moves[0]
        currency = move.get("currency_id")
        if not (isinstance(currency, (list, tuple)) and len(currency) == 2 and isinstance(currency[1], str)):
            raise ErpRepositoryResponseError(SAFE_COMPANY_AMOUNTS_ERROR)
        records = self._adapter.search_read(
            model="account.move.line",
            domain=[["move_id", "=", move_id]],
            fields=["id", "move_id", "display_type", "amount_currency", "balance"],
            limit=MAX_BILL_LINES + 1,
        )
        if len(records) > MAX_BILL_LINES:
            raise ErpRepositoryResponseError(SAFE_COMPANY_AMOUNTS_ERROR)
        return VendorBillCompanyAmounts(
            move_id=move_id,
            state=_text(move.get("state")),
            currency=currency[1].strip().upper(),
            invoice_currency_rate=_optional_decimal(move.get("invoice_currency_rate")),
            lines=tuple(_line(record, move_id) for record in records),
        )


def _line(record: dict, move_id: int) -> VendorBillCompanyLine:
    line_move = record.get("move_id")
    line_move_id = line_move[0] if isinstance(line_move, (list, tuple)) and line_move else line_move
    if not _positive_int(record.get("id")) or line_move_id != move_id:
        raise ErpRepositoryResponseError(SAFE_COMPANY_AMOUNTS_ERROR)
    return VendorBillCompanyLine(
        line_id=record["id"],
        display_type=_text(record.get("display_type")),
        amount_currency=_decimal(record.get("amount_currency")),
        balance=_decimal(record.get("balance")),
    )


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ErpRepositoryResponseError(SAFE_COMPANY_AMOUNTS_ERROR)
    return value.strip()


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or value is False:
        return None
    return _decimal(value)


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ErpRepositoryResponseError(SAFE_COMPANY_AMOUNTS_ERROR)
    try:
        parsed = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ErpRepositoryResponseError(SAFE_COMPANY_AMOUNTS_ERROR) from exc
    if not parsed.is_finite():
        raise ErpRepositoryResponseError(SAFE_COMPANY_AMOUNTS_ERROR)
    return parsed
