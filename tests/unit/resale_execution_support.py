"""Shared test doubles for Vendor Bill execution's required pre-write dependencies:
P0-PROD-18F-2's RESALE accounting check and P0-PROD-19E-2's currency precision reader."""

from __future__ import annotations

from types import SimpleNamespace


class NonResaleAccountingCheck:
    """A decision that was not accepted under RESALE: no pin, no Odoo read, no accounts."""

    def __init__(self) -> None:
        self.calls = 0

    def validate(self, source):
        self.calls += 1
        return None


class StaticCurrencyPrecisionReader:
    """A read-only currency with a fixed ``decimal_places``; records every requested code."""

    def __init__(self, decimal_places: int = 2, *, error: Exception | None = None) -> None:
        self.decimal_places = decimal_places
        self.error = error
        self.calls: list[str] = []

    def resolve_vendor_bill_currency(self, currency_code: str):
        self.calls.append(currency_code)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(currency_id=31, decimal_places=self.decimal_places)


NON_RESALE_ACCOUNTING_CHECK = NonResaleAccountingCheck()
TWO_DECIMAL_CURRENCY_READER = StaticCurrencyPrecisionReader(2)
