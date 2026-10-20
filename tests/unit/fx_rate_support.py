"""P1 FX rate guard test support: exchange-rate readers for tests that are not about FX.

``TRY_ONLY_FX_RATE_READER`` is wired into Vendor Bill strategies whose invoices are all in
TRY: the guard never reads Odoo for a TRY bill, so any call is a test bug and fails loudly.
``FixedFxRateReader`` answers every foreign-currency read with one fixed Odoo rate.
For USD tests that are not about FX, ``with_ubl_rate`` wraps a source-invoice reader so the
invoice states ``TEST_UBL_RATE`` (as the production USD invoices checked on 2026-10-20 do) and
``MATCHING_FX_RATE_READER`` answers with the same rate -- behaviour is exactly as before.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from app.billing.fx_rate import OdooDocumentRate


class TryOnlyFxRateReader:
    def resolve_vendor_bill_document_rate(
        self, *, company_id: int, currency_code: str, rate_date: date
    ) -> OdooDocumentRate:
        raise AssertionError(f"unexpected exchange-rate read for a {currency_code} bill")


class FixedFxRateReader:
    """``company_rate`` is in the UBL direction (company units per document unit)."""

    def __init__(
        self,
        company_rate: Decimal | None,
        *,
        company_currency: str = "TRY",
        decimal_places: int = 2,
        error: Exception | None = None,
    ) -> None:
        self.company_rate = company_rate
        self.company_currency = company_currency
        self.decimal_places = decimal_places
        self.error = error
        self.calls: list[tuple[int, str, date]] = []

    def resolve_vendor_bill_document_rate(
        self, *, company_id: int, currency_code: str, rate_date: date
    ) -> OdooDocumentRate:
        self.calls.append((company_id, currency_code, rate_date))
        if self.error is not None:
            raise self.error
        return OdooDocumentRate(
            company_currency=self.company_currency,
            company_currency_decimal_places=self.decimal_places,
            # Odoo's own direction and float noise, as JSON-2 returns it.
            invoice_currency_rate=(
                Decimal(repr(float(1 / self.company_rate))) if self.company_rate is not None else None
            ),
        )


TRY_ONLY_FX_RATE_READER = TryOnlyFxRateReader()


#: The J062026000000021 / move 76 rate: 1 USD = 49.1616 TRY.
TEST_UBL_RATE = Decimal("49.1616")
MATCHING_FX_RATE_READER = FixedFxRateReader(TEST_UBL_RATE)


class _WithUblRateSourceInvoiceReader:
    def __init__(self, reader, rate: Decimal) -> None:
        self._reader = reader
        self._rate = rate

    def get_source_invoice(self, **kwargs):
        source = self._reader.get_source_invoice(**kwargs)
        header = replace(source.invoice.header, exchange_rate=self._rate)
        return replace(source, invoice=replace(source.invoice, header=header))

    def __getattr__(self, name):
        return getattr(self._reader, name)


def with_ubl_rate(reader, rate: Decimal = TEST_UBL_RATE):
    return _WithUblRateSourceInvoiceReader(reader, rate)
