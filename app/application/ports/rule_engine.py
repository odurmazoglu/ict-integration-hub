from __future__ import annotations

from typing import Protocol

from app.application.commands import ImportInvoiceCommand
from app.application.dto import RuleEvaluationResult


class RuleEngine(Protocol):
    """Port for deterministic rule evaluation before workflow decisioning."""

    def evaluate(self, command: ImportInvoiceCommand) -> RuleEvaluationResult:
        pass
