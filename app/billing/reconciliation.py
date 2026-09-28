"""Source-invoice monetary totals and their currency-precision reconciliation (P0-PROD-19E-1).

The source invoice's own transmitted totals are the authoritative expectation for a
Vendor Bill: ``tax_exclusive_amount`` (untaxed), the sum of its tax amounts (tax) and
``tax_inclusive_amount`` (gross). Every comparison goes through
:func:`app.billing.money.monetary_equal`, never an absolute tolerance. A missing amount
never reconciles.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from app.billing.money import MonetaryPrecision, currency_round, monetary_equal
from app.domain.invoice import InternalInvoice, InvoiceLine


@dataclass(frozen=True, slots=True)
class SourceMonetaryTotals:
    untaxed: Decimal | None
    tax: Decimal | None
    total: Decimal | None


def source_monetary_totals(invoice: InternalInvoice) -> SourceMonetaryTotals:
    taxes = [tax.tax_amount for line in invoice.lines for tax in line.taxes]
    return SourceMonetaryTotals(
        untaxed=invoice.totals.tax_exclusive_amount,
        tax=None if any(amount is None for amount in taxes) else sum(taxes, Decimal("0")),
        total=invoice.totals.tax_inclusive_amount,
    )


def line_currency_subtotal(quantity: Decimal, unit_price: Decimal, precision: MonetaryPrecision) -> Decimal:
    """The monetary subtotal of ``quantity x unit_price`` at the currency's precision."""

    return currency_round(quantity * unit_price, precision)


def source_line_extension_amount(line: InvoiceLine) -> Decimal | None:
    return line.line_extension_amount


def monetary_mismatches(
    checks: Iterable[tuple[str, Decimal | None, Decimal | None]],
    precision: MonetaryPrecision,
) -> tuple[str, ...]:
    """Each ``(label, actual, expected)`` pair that is not the same monetary value."""

    mismatches: list[str] = []
    for label, actual, expected in checks:
        if actual is None or expected is None:
            mismatches.append(f"{label}: amount unavailable (actual={actual}, expected={expected})")
        elif not monetary_equal(actual, expected, precision):
            mismatches.append(
                f"{label}: {currency_round(actual, precision)} != {currency_round(expected, precision)} "
                f"at {precision.decimal_places} decimal places"
            )
    return tuple(mismatches)


__all__ = [
    "SourceMonetaryTotals",
    "line_currency_subtotal",
    "monetary_mismatches",
    "source_line_extension_amount",
    "source_monetary_totals",
]
