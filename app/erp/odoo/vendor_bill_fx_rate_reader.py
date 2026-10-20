"""Structurally read-only Odoo currency facts for a foreign-currency Vendor Bill (P1).

The bill is created with the invoice's own rate; this reader supplies the company
currency (direction and rounding of every TRY amount) and Odoo's *automatic* rate for
the bill date, which is displayed next to the invoice rate and never applied.

Odoo computes ``account.move.invoice_currency_rate`` for a draft bill from the bill's
``invoice_date`` via ``res.currency`` rate resolution. Reading ``res.currency.rate``
(the computed field) with ``context.date`` = that invoice date and ``context.to_currency``
= the company currency returns exactly the value Odoo puts on the bill -- including any
server-side rate-date rule, which the Hub therefore never re-implements. (Verified on
production Odoo saas~19.3: for 2026-10-06 this read returns 1/49.1616, the rate on
move 76, while the ``res.currency.rate`` record *named* 2026-10-06 holds 49.1802.)

Only ``search_read`` is used, through :class:`OdooReadOnlyAdapter`, which has no
create/write/unlink method at all: no exchange rate is ever written.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal, InvalidOperation

from app.billing.fx_rate import OdooCurrencyContext
from app.billing.money import MAX_CURRENCY_DECIMAL_PLACES
from app.erp.exceptions import ErpRepositoryError, ErpRepositoryResponseError, ErpRepositoryTimeoutError
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.write.exceptions import VendorBillWriteTransportError, VendorBillWriteUnexpectedErpError

SAFE_FX_RATE_ERROR = "Odoo exchange-rate read returned an unsafe response."


class OdooVendorBillFxRateReader:
    """Read-only; read failures are classified like the execution currency reader's
    (``OdooVendorBillExecutionCurrencyReader``): a timeout stays a retryable transport
    failure, any other ERP read failure an unexpected ERP error."""

    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def resolve_vendor_bill_document_rate(
        self, *, company_id: int, currency_code: str, rate_date: date
    ) -> OdooCurrencyContext:
        try:
            return self._resolve(company_id=company_id, currency_code=currency_code, rate_date=rate_date)
        except ErpRepositoryTimeoutError as exc:
            raise VendorBillWriteTransportError(exc.safe_message) from exc
        except ErpRepositoryError as exc:
            raise VendorBillWriteUnexpectedErpError(exc.safe_message) from exc

    def _resolve(self, *, company_id: int, currency_code: str, rate_date: date) -> OdooCurrencyContext:
        code = currency_code.strip().upper() if isinstance(currency_code, str) else ""
        if type(company_id) is not int or company_id <= 0 or not code or not isinstance(rate_date, date):
            raise ErpRepositoryResponseError(SAFE_FX_RATE_ERROR)
        company_currency_id, company_currency = self._company_currency(company_id)
        decimal_places = self._decimal_places(company_currency_id, company_currency)
        records = self._adapter.search_read(
            model="res.currency",
            domain=[["name", "=", code], ["active", "in", [True, False]]],
            fields=["id", "name", "rate"],
            limit=2,
            context={
                "date": rate_date.isoformat(),
                "to_currency": company_currency_id,
                "company_id": company_id,
                "allowed_company_ids": [company_id],
            },
        )
        if len(records) != 1 or _text(records[0].get("name")) != code:
            raise ErpRepositoryResponseError(SAFE_FX_RATE_ERROR)
        return OdooCurrencyContext(
            company_currency=company_currency,
            company_currency_decimal_places=decimal_places,
            automatic_invoice_currency_rate=_optional_rate(records[0].get("rate")),
        )

    def _company_currency(self, company_id: int) -> tuple[int, str]:
        records = self._adapter.search_read(
            model="res.company",
            domain=[["id", "=", company_id]],
            fields=["id", "currency_id"],
            limit=2,
        )
        if len(records) != 1 or records[0].get("id") != company_id:
            raise ErpRepositoryResponseError(SAFE_FX_RATE_ERROR)
        value = records[0].get("currency_id")
        if not (isinstance(value, (list, tuple)) and len(value) == 2 and _positive_int(value[0])):
            raise ErpRepositoryResponseError(SAFE_FX_RATE_ERROR)
        return value[0], _required_text(value[1]).upper()

    def _decimal_places(self, currency_id: int, currency: str) -> int:
        records = self._adapter.search_read(
            model="res.currency",
            domain=[["id", "=", currency_id], ["active", "in", [True, False]]],
            fields=["id", "name", "decimal_places"],
            limit=2,
        )
        if len(records) != 1:
            raise ErpRepositoryResponseError(SAFE_FX_RATE_ERROR)
        record = records[0]
        decimal_places = record.get("decimal_places")
        if (
            record.get("id") != currency_id
            or _text(record.get("name")) != currency
            or type(decimal_places) is not int
            or not 0 <= decimal_places <= MAX_CURRENCY_DECIMAL_PLACES
        ):
            raise ErpRepositoryResponseError(SAFE_FX_RATE_ERROR)
        return decimal_places


def _optional_rate(value: object) -> Decimal | None:
    """Odoo's float rate as Decimal via its shortest repr; None when not a positive number."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        rate = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return rate if rate > 0 else None


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _text(value: object) -> str | None:
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


def _required_text(value: object) -> str:
    text = _text(value)
    if text is None:
        raise ErpRepositoryResponseError(SAFE_FX_RATE_ERROR)
    return text
