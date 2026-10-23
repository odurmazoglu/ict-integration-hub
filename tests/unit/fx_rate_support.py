"""P1 FX rate test support: exchange-rate ports for tests that are not about FX.

``TRY_ONLY_FX_RATE_POLICY`` is wired into Vendor Bill strategies whose invoices are all in
TRY: the policy never reads anything for a TRY bill, so any read is a test bug and fails
loudly. For USD tests that are not about FX, ``with_ubl_rate`` makes the evidence state
``TEST_UBL_RATE`` (as the production USD invoices checked on 2026-10-20 do) and
``MATCHING_FX_RATE_POLICY`` reads an original document stating the same rate, then
accepts the created bill -- behaviour is otherwise exactly as before.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from app.application.execution.vendor_bill_fx_guard import VendorBillFxRatePolicy
from app.billing.fx_rate import FxVerification, FxVerificationStatus, OdooCurrencyContext
from app.domain.invoice.fx_facts import SourceExchangeRateFact, SourceFxFacts

#: The J062026000000021 / move 76 rate: 1 USD = 49.1616 TRY.
TEST_UBL_RATE = Decimal("49.1616")


class _UnexpectedRead:
    def __getattr__(self, name):
        def _fail(**kwargs):
            raise AssertionError(f"unexpected exchange-rate read {name}({kwargs})")

        return _fail


class FixedFxRateReader:
    """Odoo currency context; ``company_rate`` is Odoo's automatic rate in the UBL direction."""

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
    ) -> OdooCurrencyContext:
        self.calls.append((company_id, currency_code, rate_date))
        if self.error is not None:
            raise self.error
        return OdooCurrencyContext(
            company_currency=self.company_currency,
            company_currency_decimal_places=self.decimal_places,
            # Odoo's own direction and float noise, as JSON-2 returns it.
            automatic_invoice_currency_rate=(
                Decimal(repr(float(1 / self.company_rate))) if self.company_rate is not None else None
            ),
        )


def pricing_rate(
    rate: str | None, *, source: str | None = "USD", target: str | None = "TRY", element: str = "PricingExchangeRate"
) -> SourceExchangeRateFact:
    try:
        value = Decimal(rate) if rate is not None else None
    except ArithmeticError:
        value = None
    return SourceExchangeRateFact(
        element=element,
        source_currency=source,
        target_currency=target,
        calculation_rate_text=rate,
        calculation_rate=value,
    )


class FixedSourceFxFactsReader:
    """The verified original document states ``rates`` (or is unverified)."""

    def __init__(self, *rates: SourceExchangeRateFact, currency: str = "USD", unverified: bool = False, error=None):
        self.rates = rates
        self.currency = currency
        self.unverified = unverified
        self.error = error
        self.calls: list[tuple[str, int, str | None]] = []

    def read_source_fx_facts(self, *, review_id: str, company_id: int, invoice_uuid: str | None):
        self.calls.append((review_id, company_id, invoice_uuid))
        if self.error is not None:
            raise self.error
        if self.unverified:
            return None
        return SourceFxFacts(invoice_uuid=invoice_uuid, document_currency=self.currency, exchange_rates=self.rates)


class _AcceptingFxRatePolicy(VendorBillFxRatePolicy):
    def verify_created(self, plan, *, move_id: int, company_id: int) -> FxVerification:
        return FxVerification(status=FxVerificationStatus.VERIFIED, expected_rate=plan.selected_rate)


TRY_ONLY_FX_RATE_POLICY = VendorBillFxRatePolicy(
    facts_reader=_UnexpectedRead(), currency_reader=_UnexpectedRead(), amounts_reader=_UnexpectedRead()
)
MATCHING_FX_RATE_POLICY = _AcceptingFxRatePolicy(
    facts_reader=FixedSourceFxFactsReader(pricing_rate(str(TEST_UBL_RATE))),
    currency_reader=FixedFxRateReader(TEST_UBL_RATE),
    amounts_reader=_UnexpectedRead(),
)


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
