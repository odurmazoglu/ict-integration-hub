"""Foreign-currency Vendor Bill exchange rate (P1): the invoice rate is authoritative.

Business rule: for a foreign-currency supplier invoice, the exchange rate explicitly and
validly stated in the original UBL invoice is the accounting rate. The Hub applies it to
the one new draft Vendor Bill through standard Odoo ``account.move.invoice_currency_rate``
and never substitutes Odoo's automatic (TCMB) rate. ``res.currency.rate`` is never
written; no other bill is touched.

Rate-selection rule (:func:`select_vendor_bill_rate`)
-----------------------------------------------------
1. Bill in the company currency (TRY) -> ``not_applicable``: no rate is sent.
2. The original stored UBL must be verified (hash + UUID) -> else ``unverified_source_document``.
3. Its ``DocumentCurrencyCode`` must equal the evidence currency -> else ``source_rate_contradictory``.
4. Exactly one invoice-level ``PricingExchangeRate`` (the UBL-TR rate of the document
   currency) -> none: ``source_rate_missing``; several: ``source_rate_contradictory``.
   Payment/Tax exchange rates are never taken as the accounting rate.
5. Its ``SourceCurrencyCode`` must be the bill currency and ``TargetCurrencyCode`` the
   company currency, both stated -> else ``source_rate_direction_invalid`` (never inverted).
6. ``CalculationRate`` must be a finite, positive number -> else ``source_rate_invalid``.
7. It must equal the rate the accepted decision's evidence holds -> else ``source_rate_contradictory``.
8. Any other invoice-level exchange rate for the same currency pair must state the same
   rate -> else ``source_rate_contradictory``.
Then the exact Decimal ``CalculationRate`` is selected (``source_rate_selected``). Odoo's
automatic rate for the bill date is read for display only; a difference is reported,
never applied, and does not block.

Direction: UBL states company-currency units per one document-currency unit
(1 USD = 49.1616 TRY). Odoo's ``invoice_currency_rate`` is the inverse ("currency rate
from company currency to document currency", 0.0203410792162989); Odoo converts every
line as ``amount_currency / invoice_currency_rate`` rounded at the company currency.

Free-text invoice notes are never parsed. Decimal only; the value sent to Odoo is the
nearest binary float of ``1 / rate`` because the Odoo field is a float.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import StrEnum

from app.billing.money import MonetaryPrecision, MonetaryPrecisionError, currency_round
from app.domain.invoice.fx_facts import PRICING_EXCHANGE_RATE, SourceFxFacts

#: Rates are *displayed and compared with Odoo's automatic rate* at 6 decimals (UBL-TR
#: CalculationRate carries up to 6, TCMB publishes 4); the selected rate is never rounded.
RATE_DECIMAL_PLACES = 6
RATE_QUANTUM = Decimal(1).scaleb(-RATE_DECIMAL_PLACES)


class FxRateStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    SOURCE_RATE_SELECTED = "source_rate_selected"
    UNVERIFIED_SOURCE_DOCUMENT = "unverified_source_document"
    SOURCE_RATE_MISSING = "source_rate_missing"
    SOURCE_RATE_INVALID = "source_rate_invalid"
    SOURCE_RATE_DIRECTION_INVALID = "source_rate_direction_invalid"
    SOURCE_RATE_CONTRADICTORY = "source_rate_contradictory"
    #: The company currency (or, in preview, any evidence) could not be read right now.
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"


@dataclass(frozen=True, slots=True)
class OdooCurrencyContext:
    """Read-only Odoo facts for one bill: the company currency and its precision, and
    Odoo's *automatic* rate for the bill date (Odoo direction; None when unusable)."""

    company_currency: str
    company_currency_decimal_places: int
    automatic_invoice_currency_rate: Decimal | None


@dataclass(frozen=True, slots=True)
class FxRatePlan:
    status: FxRateStatus
    document_currency: str
    company_currency: str | None = None
    rate_date: date | None = None
    #: The UBL PricingExchangeRate CalculationRate, exact (company units per document unit).
    source_rate: Decimal | None = None
    #: Odoo's automatic rate for rate_date, same direction, normalized; display only.
    odoo_automatic_rate: Decimal | None = None
    #: The rate the new bill is created with (== source_rate when selected).
    selected_rate: Decimal | None = None
    #: The exact float sent as ``invoice_currency_rate`` (Odoo direction).
    odoo_invoice_currency_rate: float | None = None
    #: selected_rate - odoo_automatic_rate (normalized), when both are known.
    rate_difference: Decimal | None = None
    company_currency_decimal_places: int | None = None
    expected_company_untaxed: Decimal | None = None
    expected_company_tax: Decimal | None = None
    expected_company_total: Decimal | None = None
    message: str = ""

    @property
    def applies_rate(self) -> bool:
        return self.status is FxRateStatus.SOURCE_RATE_SELECTED

    @property
    def blocks_vendor_bill(self) -> bool:
        return self.status not in {FxRateStatus.NOT_APPLICABLE, FxRateStatus.SOURCE_RATE_SELECTED}


def odoo_invoice_currency_rate(rate: Decimal) -> float:
    """The Odoo ``invoice_currency_rate`` for a UBL-direction rate: the nearest float of 1/rate."""

    if not isinstance(rate, Decimal) or not rate.is_finite() or rate <= 0:
        raise ValueError("A finite positive Decimal rate is required.")
    with localcontext() as context:
        context.prec = 40
        return float(Decimal(1) / rate)


def company_rate_from_odoo(invoice_currency_rate: Decimal | None) -> Decimal | None:
    """An Odoo-direction rate inverted to the UBL direction and normalized for display."""

    if invoice_currency_rate is None or not invoice_currency_rate.is_finite() or invoice_currency_rate <= 0:
        return None
    with localcontext() as context:
        context.prec = 40
        inverted = Decimal(1) / invoice_currency_rate
    return inverted.quantize(RATE_QUANTUM, rounding=ROUND_HALF_EVEN)


def normalized_rate(rate: Decimal) -> Decimal:
    return rate.quantize(RATE_QUANTUM, rounding=ROUND_HALF_EVEN)


def select_vendor_bill_rate(
    *,
    document_currency: str,
    rate_date: date | None,
    evidence_rate: Decimal | None,
    facts: SourceFxFacts | None,
    odoo: OdooCurrencyContext | None,
    line_subtotals: Iterable[Decimal] = (),
    tax_total: Decimal | None = None,
) -> FxRatePlan:
    """Apply the rate-selection rule (module docstring). Pure; never raises for data."""

    document = (document_currency or "").strip().upper()
    company = odoo.company_currency.strip().upper() if odoo is not None else None
    precision = _precision(odoo)
    automatic = _automatic_rate(odoo)
    base = {
        "document_currency": document,
        "company_currency": company,
        "rate_date": rate_date,
        "odoo_automatic_rate": automatic,
        "company_currency_decimal_places": precision.decimal_places if precision is not None else None,
    }
    if company is not None and document == company:
        return FxRatePlan(status=FxRateStatus.NOT_APPLICABLE, message="Bill is in the company currency.", **base)
    if company is None or precision is None:
        return _blocked(
            FxRateStatus.EVIDENCE_UNAVAILABLE,
            "The Odoo company currency could not be read; no Vendor Bill was created. Retry.",
            base,
        )
    if facts is None:
        return _blocked(
            FxRateStatus.UNVERIFIED_SOURCE_DOCUMENT,
            "The original invoice document could not be verified, so its exchange rate cannot be trusted. "
            "No Vendor Bill was created.",
            base,
        )
    if facts.document_currency != document:
        return _blocked(
            FxRateStatus.SOURCE_RATE_CONTRADICTORY,
            f"The original invoice currency {facts.document_currency} differs from the accepted evidence "
            f"currency {document}. No Vendor Bill was created.",
            base,
        )
    pricing = facts.by_element(PRICING_EXCHANGE_RATE)
    if not pricing:
        return _blocked(
            FxRateStatus.SOURCE_RATE_MISSING,
            f"The original invoice states no PricingExchangeRate for {document}; the Hub does not use Odoo's "
            "automatic rate instead. No Vendor Bill was created. Ask accounting which rate applies.",
            base,
        )
    if len(pricing) > 1:
        return _blocked(
            FxRateStatus.SOURCE_RATE_CONTRADICTORY,
            "The original invoice states more than one PricingExchangeRate. No Vendor Bill was created.",
            base,
        )
    stated = pricing[0]
    if stated.source_currency != document or stated.target_currency != company:
        return _blocked(
            FxRateStatus.SOURCE_RATE_DIRECTION_INVALID,
            f"The invoice PricingExchangeRate converts {stated.source_currency} -> {stated.target_currency}, "
            f"not {document} -> {company}; the Hub never inverts or guesses a rate direction. "
            "No Vendor Bill was created.",
            base,
        )
    rate = stated.calculation_rate
    if rate is None or not rate.is_finite() or rate <= 0:
        return _blocked(
            FxRateStatus.SOURCE_RATE_INVALID,
            f"The invoice PricingExchangeRate CalculationRate {stated.calculation_rate_text!r} is not a positive "
            "number. No Vendor Bill was created.",
            base,
        )
    base["source_rate"] = rate
    if evidence_rate is None or evidence_rate != rate:
        return _blocked(
            FxRateStatus.SOURCE_RATE_CONTRADICTORY,
            f"The original invoice rate {rate} differs from the accepted decision evidence rate {evidence_rate}. "
            "No Vendor Bill was created.",
            base,
        )
    for other in facts.exchange_rates:
        if other is stated or (other.source_currency, other.target_currency) != (document, company):
            continue
        if other.calculation_rate is None or other.calculation_rate != rate:
            return _blocked(
                FxRateStatus.SOURCE_RATE_CONTRADICTORY,
                f"The invoice {other.element} ({other.calculation_rate_text}) contradicts its PricingExchangeRate "
                f"({rate}) for {document} -> {company}. No Vendor Bill was created.",
                base,
            )

    lines = tuple(convert_to_company_currency(subtotal, rate, precision) for subtotal in line_subtotals)
    untaxed = sum(lines, Decimal("0"))
    tax = convert_to_company_currency(tax_total, rate, precision) if tax_total is not None else None
    difference = normalized_rate(rate) - automatic if automatic is not None else None
    automatic_text = (
        f"Odoo's automatic rate for {rate_date} is {automatic} {company}"
        + (" (same)." if difference == 0 else f" (difference {difference}); it is not used.")
        if automatic is not None
        else "Odoo's automatic rate is unavailable; it is not used."
    )
    return FxRatePlan(
        status=FxRateStatus.SOURCE_RATE_SELECTED,
        selected_rate=rate,
        odoo_invoice_currency_rate=odoo_invoice_currency_rate(rate),
        rate_difference=difference,
        expected_company_untaxed=untaxed,
        expected_company_tax=tax,
        expected_company_total=untaxed + tax if tax is not None else None,
        message=f"The bill is created with the invoice rate 1 {document} = {rate} {company}. {automatic_text}",
        **base,
    )


def convert_to_company_currency(amount: Decimal, rate: Decimal, precision: MonetaryPrecision) -> Decimal:
    """``amount x rate`` at the company currency's precision, half away from zero (Odoo float_round)."""

    return currency_round(amount * rate, precision)


# ---------------------------------------------------------------- created-bill verification


class FxVerificationStatus(StrEnum):
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    NOT_APPLICABLE = "not_applicable"
    #: The bill or the plan could not be read, so nothing is proven.
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class VendorBillCompanyLine:
    """One Odoo journal item of the bill: its type and its document/company amounts (signed)."""

    line_id: int
    display_type: str
    amount_currency: Decimal
    balance: Decimal


@dataclass(frozen=True, slots=True)
class VendorBillCompanyAmounts:
    move_id: int
    state: str
    currency: str
    invoice_currency_rate: Decimal | None
    lines: tuple[VendorBillCompanyLine, ...]


@dataclass(frozen=True, slots=True)
class FxVerification:
    status: FxVerificationStatus
    expected_rate: Decimal | None = None
    #: The bill's actual rate in the UBL direction, normalized.
    actual_rate: Decimal | None = None
    company_untaxed: Decimal | None = None
    company_tax: Decimal | None = None
    company_payable: Decimal | None = None
    expected_company_total: Decimal | None = None
    mismatches: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)


#: Journal items Odoo creates for a plain Vendor Bill. Any other type (cash rounding,
#: early-payment discount, non-deductible tax ...) is outside what is verified: fail closed.
PRODUCT_LINE, TAX_LINE, PAYABLE_LINE = "product", "tax", "payment_term"
_ZERO_LINE_TYPES = frozenset({"line_section", "line_subsection", "line_note"})


def verify_vendor_bill_company_amounts(
    plan: FxRatePlan | None, actual: VendorBillCompanyAmounts | None
) -> FxVerification:
    """Prove a created draft bill carries the selected rate and Odoo's TRY amounts follow it.

    * ``invoice_currency_rate``: its inverse equals the selected rate at 6 decimals;
    * each product line: ``|balance| = round(|amount_currency| x rate)`` within one quantum
      (Odoo divides by a float rate);
    * tax lines: the same, within one quantum per product line (Odoo may round per line);
    * payable: it balances every other line exactly, and equals the expected TRY total
      within the summed tolerances above.
    """

    if plan is None or plan.status is not FxRateStatus.SOURCE_RATE_SELECTED:
        if plan is not None and plan.status is FxRateStatus.NOT_APPLICABLE:
            return FxVerification(status=FxVerificationStatus.NOT_APPLICABLE)
        return FxVerification(status=FxVerificationStatus.UNVERIFIED, mismatches=("no applicable invoice rate",))
    if actual is None:
        return FxVerification(
            status=FxVerificationStatus.UNVERIFIED, expected_rate=plan.selected_rate, mismatches=("bill not found",)
        )
    rate = plan.selected_rate
    assert rate is not None
    try:
        precision = MonetaryPrecision(plan.company_currency_decimal_places)  # type: ignore[arg-type]
    except MonetaryPrecisionError:
        return FxVerification(status=FxVerificationStatus.UNVERIFIED, mismatches=("company precision unknown",))
    quantum = precision.quantum
    mismatches: list[str] = []
    notes: list[str] = []
    actual_rate = company_rate_from_odoo(actual.invoice_currency_rate)
    if actual.currency.strip().upper() != plan.document_currency:
        mismatches.append(f"bill currency {actual.currency} != {plan.document_currency}")
    if actual.state != "draft":
        mismatches.append(f"bill state {actual.state} != draft")
    if actual_rate is None or actual_rate != normalized_rate(rate):
        mismatches.append(f"bill rate {actual_rate} != invoice rate {rate}")

    by_type: dict[str, list[VendorBillCompanyLine]] = {}
    for line in actual.lines:
        if line.display_type in _ZERO_LINE_TYPES:
            continue
        if line.display_type not in {PRODUCT_LINE, TAX_LINE, PAYABLE_LINE}:
            mismatches.append(f"line {line.line_id}: unverified line type {line.display_type}")
            continue
        by_type.setdefault(line.display_type, []).append(line)
    products = by_type.get(PRODUCT_LINE, [])
    taxes = by_type.get(TAX_LINE, [])
    payables = by_type.get(PAYABLE_LINE, [])
    if not products or not payables:
        mismatches.append("bill has no product or payable line")

    tax_tolerance = quantum * max(1, len(products))
    for lines, tolerance in ((products, quantum), (taxes, tax_tolerance)):
        for line in lines:
            expected = convert_to_company_currency(abs(line.amount_currency), rate, precision)
            difference = abs(line.balance) - expected
            if abs(difference) > tolerance:
                mismatches.append(f"line {line.line_id}: TRY {abs(line.balance)} != expected {expected}")
            elif difference:
                notes.append(f"line {line.line_id}: TRY differs by {difference} (rounding, within {tolerance})")
    untaxed = sum((abs(line.balance) for line in products), Decimal("0"))
    tax = sum((abs(line.balance) for line in taxes), Decimal("0"))
    debit_side = sum((line.balance for line in products + taxes), Decimal("0"))
    payable = sum((line.balance for line in payables), Decimal("0"))
    if payable + debit_side != 0:
        mismatches.append(f"payable {payable} does not balance the lines {debit_side}")
    if plan.expected_company_total is not None:
        total_tolerance = quantum * len(products) + tax_tolerance
        difference = abs(payable) - plan.expected_company_total
        if abs(difference) > total_tolerance:
            mismatches.append(f"TRY payable {abs(payable)} != expected {plan.expected_company_total}")
        elif difference:
            notes.append(f"TRY payable differs by {difference} (rounding, within {total_tolerance})")
    return FxVerification(
        status=FxVerificationStatus.MISMATCH if mismatches else FxVerificationStatus.VERIFIED,
        expected_rate=rate,
        actual_rate=actual_rate,
        company_untaxed=untaxed,
        company_tax=tax,
        company_payable=abs(payable),
        expected_company_total=plan.expected_company_total,
        mismatches=tuple(mismatches),
        notes=tuple(notes),
    )


def _blocked(status: FxRateStatus, message: str, base: dict) -> FxRatePlan:
    return FxRatePlan(status=status, message=message, **base)


def _precision(odoo: OdooCurrencyContext | None) -> MonetaryPrecision | None:
    if odoo is None:
        return None
    try:
        return MonetaryPrecision(odoo.company_currency_decimal_places)
    except MonetaryPrecisionError:
        return None


def _automatic_rate(odoo: OdooCurrencyContext | None) -> Decimal | None:
    automatic = company_rate_from_odoo(odoo.automatic_invoice_currency_rate) if odoo is not None else None
    # Odoo falls back to 1.0 when it holds no rate at all for the currency.
    return None if automatic == normalized_rate(Decimal(1)) else automatic


__all__ = [
    "RATE_DECIMAL_PLACES",
    "FxRatePlan",
    "FxRateStatus",
    "FxVerification",
    "FxVerificationStatus",
    "OdooCurrencyContext",
    "VendorBillCompanyAmounts",
    "VendorBillCompanyLine",
    "company_rate_from_odoo",
    "convert_to_company_currency",
    "normalized_rate",
    "odoo_invoice_currency_rate",
    "select_vendor_bill_rate",
    "verify_vendor_bill_company_amounts",
]
