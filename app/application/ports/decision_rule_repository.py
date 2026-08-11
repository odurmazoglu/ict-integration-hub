from __future__ import annotations

from typing import Protocol

from app.application.rules import InvoiceDecisionRule


class DecisionRuleRepository(Protocol):
    """Read-only port for immutable invoice decision rules authored outside Hub."""

    def list_invoice_decision_rules(self, *, company_id: int) -> tuple[InvoiceDecisionRule, ...]:
        pass
