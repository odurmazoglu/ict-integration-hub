"""Foreign-currency Vendor Bill exchange-rate safety (P1 FX rate guard).

A Vendor Bill in a foreign currency is converted to the company currency by Odoo, with
the rate Odoo itself resolves for the bill's ``invoice_date`` (``account.move``
``invoice_currency_rate``). The Hub never sends or changes that rate. This module only
*compares* it with the rate the original UBL invoice states, so a supplier who used a
different rate can never pass silently.

Rates are compared as "company-currency units per one document-currency unit" -- the
UBL-TR ``CalculationRate`` direction (SourceCurrencyCode = document currency,
TargetCurrencyCode = TRY). The direction is proven by the value itself: a rate is accepted
only when it equals Odoo's document -> company rate, which a reversed rate (0.0203 instead
of 49.16) can never do; a reversed rate is still diagnosed as such (its product with Odoo's
rate is ~1) so the operator sees why. Odoo stores the inverse
(``invoice_currency_rate`` = document units per company unit, a binary float), so the
Odoo side is inverted with Decimal and normalized to :data:`RATE_DECIMAL_PLACES` places
to remove float noise (``48.674899999999994`` -> ``48.674900``).

A rate pair matches only when both normalized rates are equal *and* the document total
converts to the same company-currency amount at the company currency's precision under
both rates (half away from zero, like Odoo's ``float_round``). Free-text invoice notes
(e.g. "IKIBINKIRKBIR TL ONDOKUZ Kr.") are never parsed: they are not a validated
source format.

Decimal only, never binary floats.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from enum import StrEnum

from app.billing.money import MonetaryPrecision, currency_round

#: UBL-TR CalculationRate carries up to 6 decimals (e.g. ``49.161600``); TCMB publishes 4.
RATE_DECIMAL_PLACES = 6
RATE_QUANTUM = Decimal(1).scaleb(-RATE_DECIMAL_PLACES)
#: A UBL rate whose product with Odoo's rate is within 0.1% of 1 is the reciprocal rate.
RECIPROCAL_TOLERANCE = Decimal("0.001")


class FxRateCheckStatus(StrEnum):
    #: The bill is in the company currency; no conversion happens.
    NOT_APPLICABLE = "not_applicable"
    MATCH = "match"
    #: Both rates are known and they, or the converted totals, differ.
    MISMATCH = "mismatch"
    #: The UBL invoice states no usable exchange rate.
    SOURCE_RATE_MISSING = "source_rate_missing"
    #: The UBL rate is stated in the reversed (company -> document currency) direction.
    SOURCE_RATE_DIRECTION_INVALID = "source_rate_direction_invalid"
    #: Odoo's rate for the bill date could not be determined.
    ODOO_RATE_UNAVAILABLE = "odoo_rate_unavailable"


@dataclass(frozen=True, slots=True)
class OdooDocumentRate:
    """What Odoo will apply to a bill dated ``rate_date`` (read-only resolution).

    ``invoice_currency_rate`` is in Odoo's own direction: document-currency units per one
    company-currency unit (``res.currency.rate`` resolved for the bill date). ``None``
    when Odoo returned no usable rate.
    """

    company_currency: str
    company_currency_decimal_places: int
    invoice_currency_rate: Decimal | None


@dataclass(frozen=True, slots=True)
class FxRateCheck:
    status: FxRateCheckStatus
    document_currency: str
    company_currency: str | None
    rate_date: date | None
    source_rate: Decimal | None = None
    #: Odoo's rate in the UBL direction, normalized to RATE_DECIMAL_PLACES.
    odoo_rate: Decimal | None = None
    document_total: Decimal | None = None
    company_currency_decimal_places: int | None = None
    source_company_total: Decimal | None = None
    odoo_company_total: Decimal | None = None
    company_total_difference: Decimal | None = None
    message: str = ""

    @property
    def blocks_vendor_bill(self) -> bool:
        return self.status not in {FxRateCheckStatus.NOT_APPLICABLE, FxRateCheckStatus.MATCH}


def company_rate_from_odoo(invoice_currency_rate: Decimal | None) -> Decimal | None:
    """Odoo's ``invoice_currency_rate`` inverted to the UBL direction and normalized."""

    if invoice_currency_rate is None or not invoice_currency_rate.is_finite() or invoice_currency_rate <= 0:
        return None
    with localcontext() as context:
        context.prec = 40
        inverted = Decimal(1) / invoice_currency_rate
    return inverted.quantize(RATE_QUANTUM, rounding=ROUND_HALF_EVEN)


def normalized_rate(rate: Decimal) -> Decimal:
    return rate.quantize(RATE_QUANTUM, rounding=ROUND_HALF_EVEN)


def convert_to_company_currency(amount: Decimal, rate: Decimal, precision: MonetaryPrecision) -> Decimal:
    return currency_round(amount * rate, precision)


def evaluate_vendor_bill_fx_rate(
    *,
    document_currency: str,
    rate_date: date | None,
    source_rate: Decimal | None,
    document_total: Decimal | None,
    odoo_rate: OdooDocumentRate | None,
) -> FxRateCheck:
    """Compare the UBL rate with the rate Odoo will apply. Pure; never raises for data."""

    document = (document_currency or "").strip().upper()
    company = odoo_rate.company_currency.strip().upper() if odoo_rate is not None else None
    base = {
        "document_currency": document,
        "company_currency": company,
        "rate_date": rate_date,
        "source_rate": source_rate,
        "document_total": document_total,
    }
    if company is not None and document == company:
        return FxRateCheck(status=FxRateCheckStatus.NOT_APPLICABLE, message="Bill is in the company currency.", **base)

    precision = _precision(odoo_rate)
    odoo_company_rate = company_rate_from_odoo(odoo_rate.invoice_currency_rate) if odoo_rate is not None else None
    if odoo_company_rate is not None and odoo_company_rate == normalized_rate(Decimal(1)):
        # Odoo falls back to 1.0 when it holds no rate at all for the currency.
        odoo_company_rate = None
    base.update(odoo_rate=odoo_company_rate, company_currency_decimal_places=_decimal_places(precision))

    if not _usable_rate(source_rate):
        return FxRateCheck(
            status=FxRateCheckStatus.SOURCE_RATE_MISSING,
            message=(
                f"The original invoice states no usable {document} exchange rate (PricingExchangeRate/"
                "CalculationRate); the Odoo rate cannot be confirmed. No Vendor Bill was created. "
                "Ask accounting which rate applies before creating this bill."
            ),
            **base,
        )
    if company is None or precision is None or odoo_company_rate is None:
        return FxRateCheck(
            status=FxRateCheckStatus.ODOO_RATE_UNAVAILABLE,
            message=(
                f"Odoo's {document} exchange rate for {rate_date} could not be determined. No Vendor Bill was "
                "created; check the Odoo currency rates for that date and retry."
            ),
            **base,
        )

    source_company_total = odoo_company_total = difference = None
    if document_total is not None and document_total.is_finite():
        source_company_total = convert_to_company_currency(document_total, source_rate, precision)
        odoo_company_total = convert_to_company_currency(document_total, odoo_company_rate, precision)
        difference = odoo_company_total - source_company_total
    base.update(
        source_company_total=source_company_total,
        odoo_company_total=odoo_company_total,
        company_total_difference=difference,
    )
    if normalized_rate(source_rate) == odoo_company_rate and not difference:
        return FxRateCheck(status=FxRateCheckStatus.MATCH, message="Invoice and Odoo exchange rates match.", **base)
    if abs(source_rate * odoo_company_rate - 1) <= RECIPROCAL_TOLERANCE:
        return FxRateCheck(
            status=FxRateCheckStatus.SOURCE_RATE_DIRECTION_INVALID,
            message=(
                f"The invoice exchange rate {source_rate} is stated in the reversed direction (1 {company} = "
                f"{source_rate} {document}); Odoo's rate is 1 {document} = {odoo_company_rate} {company}. The Hub "
                f"does not invert rates. No Vendor Bill was created. Ask accounting to confirm the {document} -> "
                f"{company} rate."
            ),
            **base,
        )
    totals = (
        f" The {document_total} {document} total would be {odoo_company_total} {company} in Odoo instead of "
        f"{source_company_total} {company} (difference {difference} {company})."
        if difference is not None
        else ""
    )
    return FxRateCheck(
        status=FxRateCheckStatus.MISMATCH,
        message=(
            f"The invoice exchange rate 1 {document} = {source_rate} {company} differs from Odoo's rate "
            f"1 {document} = {odoo_company_rate} {company} for {rate_date}.{totals} No Vendor Bill was created. "
            "The Hub does not change exchange rates: ask accounting to confirm the rate, and create this bill "
            "manually in Odoo with the invoice rate if they approve it."
        ),
        **base,
    )


def _usable_rate(rate: Decimal | None) -> bool:
    return isinstance(rate, Decimal) and rate.is_finite() and rate > 0


def _precision(odoo_rate: OdooDocumentRate | None) -> MonetaryPrecision | None:
    if odoo_rate is None:
        return None
    try:
        return MonetaryPrecision(odoo_rate.company_currency_decimal_places)
    except (ValueError, InvalidOperation):
        return None


def _decimal_places(precision: MonetaryPrecision | None) -> int | None:
    return precision.decimal_places if precision is not None else None


__all__ = [
    "RATE_DECIMAL_PLACES",
    "FxRateCheck",
    "FxRateCheckStatus",
    "OdooDocumentRate",
    "company_rate_from_odoo",
    "convert_to_company_currency",
    "evaluate_vendor_bill_fx_rate",
    "normalized_rate",
]
