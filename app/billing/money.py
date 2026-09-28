"""Canonical monetary reconciliation rule (P0-PROD-19E-1).

Two amounts reconcile when they are equal *at the invoice currency's precision*:

    currency_round(a) == currency_round(b)

never when they merely lie within an absolute tolerance. ``2 x 59.7378 = 119.4756``
and a source ``LineExtensionAmount`` of ``119.48`` are the same USD amount; ``119.46``
is not. Rounding is half away from zero, the semantics of Odoo's ``float_round`` that
produces the bill's stored monetary values.

Decimal only, never binary floats. The precision is always supplied by the caller from
the currency's own configuration (Odoo ``res.currency.decimal_places``) -- this module
never assumes a currency has two decimals.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

MAX_CURRENCY_DECIMAL_PLACES = 6


class MonetaryPrecisionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MonetaryPrecision:
    """A currency's monetary precision: the number of decimal places it stores."""

    decimal_places: int

    def __post_init__(self) -> None:
        if type(self.decimal_places) is not int or not 0 <= self.decimal_places <= MAX_CURRENCY_DECIMAL_PLACES:
            raise MonetaryPrecisionError("Currency decimal_places must be an integer between 0 and 6.")

    @property
    def quantum(self) -> Decimal:
        return Decimal(1).scaleb(-self.decimal_places)


def currency_round(amount: Decimal, precision: MonetaryPrecision) -> Decimal:
    """``amount`` at the currency's precision, rounding half away from zero."""

    if not isinstance(amount, Decimal) or not amount.is_finite():
        raise MonetaryPrecisionError("A finite Decimal amount is required.")
    if not isinstance(precision, MonetaryPrecision):
        raise MonetaryPrecisionError("A MonetaryPrecision is required.")
    return amount.quantize(precision.quantum, rounding=ROUND_HALF_UP)


def monetary_equal(left: Decimal, right: Decimal, precision: MonetaryPrecision) -> bool:
    """True when both amounts are the same monetary value at the currency's precision."""

    return currency_round(left, precision) == currency_round(right, precision)


__all__ = [
    "MAX_CURRENCY_DECIMAL_PLACES",
    "MonetaryPrecision",
    "MonetaryPrecisionError",
    "currency_round",
    "monetary_equal",
]
