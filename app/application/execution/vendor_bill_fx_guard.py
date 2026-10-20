"""Foreign-currency Vendor Bill exchange-rate guard (P1), shared by EXECUTE and preview.

EXECUTE runs it after the bill is built and before the writer's first call, on every
attempt (DRY_RUN and EXECUTE, retry and resume); any blocking result fails the step
closed, so nothing is written and no Vendor Bill -- let alone a duplicate -- exists for
a mismatched invoice. Preview reports the same check without raising.

The Hub only compares rates (``app.billing.fx_rate``); it never sends an exchange rate
to Odoo and never writes ``res.currency.rate``.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from app.application.exceptions import ApplicationError
from app.billing.fx_rate import FxRateCheck, FxRateCheckStatus, OdooDocumentRate, evaluate_vendor_bill_fx_rate
from app.billing.reconciliation import source_monetary_totals
from app.domain.invoice.dto import InternalInvoice

#: Turkish e-invoices for a TRY company: a TRY bill is never converted, so it is checked
#: without any Odoo read and its execution keeps its exact pre-guard Odoo call sequence.
UNCONVERTED_CURRENCY_CODE = "TRY"


class VendorBillFxRateReader(Protocol):
    """Structurally read-only: resolves the rate Odoo applies for a bill date. No write method."""

    def resolve_vendor_bill_document_rate(
        self, *, company_id: int, currency_code: str, rate_date: date
    ) -> OdooDocumentRate:
        pass


class VendorBillFxRateBlockedError(ApplicationError):
    """A foreign-currency bill whose exchange rate is missing, invalid or mismatched."""

    error_category = "vendor_bill_fx_rate_blocked"

    def __init__(self, check: FxRateCheck) -> None:
        super().__init__(check.message)
        self.check = check

    @property
    def error_code(self) -> str:
        return f"vendor_bill_fx_{self.check.status.value}"


def vendor_bill_fx_rate_check(
    reader: VendorBillFxRateReader,
    invoice: InternalInvoice,
    *,
    company_id: int,
    rate_date: date,
) -> FxRateCheck:
    """Evaluate the invoice's UBL rate against Odoo's rate for ``rate_date``.

    ``rate_date`` must be the bill's ``invoice_date`` exactly as sent to Odoo. Read
    failures propagate unchanged (EXECUTE then fails through its normal error mapping).
    """

    currency = (invoice.header.currency_code or "").strip().upper()
    odoo_rate = (
        None
        if currency == UNCONVERTED_CURRENCY_CODE
        else reader.resolve_vendor_bill_document_rate(
            company_id=company_id, currency_code=currency, rate_date=rate_date
        )
    )
    return _evaluate(invoice, currency=currency, rate_date=rate_date, odoo_rate=odoo_rate)


def unavailable_vendor_bill_fx_rate_check(invoice: InternalInvoice, *, rate_date: date) -> FxRateCheck:
    """The check when Odoo's rate could not be read (preview reports it instead of raising)."""

    currency = (invoice.header.currency_code or "").strip().upper()
    return _evaluate(invoice, currency=currency, rate_date=rate_date, odoo_rate=None)


def ensure_vendor_bill_fx_rate_allowed(
    reader: VendorBillFxRateReader,
    invoice: InternalInvoice,
    *,
    company_id: int,
    rate_date: date,
) -> FxRateCheck:
    check = vendor_bill_fx_rate_check(reader, invoice, company_id=company_id, rate_date=rate_date)
    if check.blocks_vendor_bill:
        raise VendorBillFxRateBlockedError(check)
    return check


def _evaluate(
    invoice: InternalInvoice, *, currency: str, rate_date: date, odoo_rate: OdooDocumentRate | None
) -> FxRateCheck:
    if currency == UNCONVERTED_CURRENCY_CODE:
        return FxRateCheck(
            status=FxRateCheckStatus.NOT_APPLICABLE,
            document_currency=currency,
            company_currency=UNCONVERTED_CURRENCY_CODE,
            rate_date=rate_date,
            message="Bill is in the company currency.",
        )
    header = invoice.header
    return evaluate_vendor_bill_fx_rate(
        document_currency=currency,
        rate_date=rate_date,
        source_rate=header.exchange_rate,
        # The amount the bill itself totals to (and reconciles against), not PayableAmount.
        document_total=source_monetary_totals(invoice).total,
        odoo_rate=odoo_rate,
    )
