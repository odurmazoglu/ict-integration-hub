"""Shared test double for P0-PROD-18F-2's required RESALE execution accounting check."""

from __future__ import annotations


class NonResaleAccountingCheck:
    """A decision that was not accepted under RESALE: no pin, no Odoo read, no accounts."""

    def __init__(self) -> None:
        self.calls = 0

    def validate(self, source):
        self.calls += 1
        return None


NON_RESALE_ACCOUNTING_CHECK = NonResaleAccountingCheck()
