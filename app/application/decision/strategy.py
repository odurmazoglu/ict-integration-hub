from __future__ import annotations

from typing import Protocol

from app.application.commands import ImportInvoiceCommand
from app.application.dto import DecisionResult, RuleEvaluationResult


class WorkflowStrategy(Protocol):
    """Executable workflow selected by the Decision Engine."""

    workflow: str
    name: str

    async def execute(self, command: ImportInvoiceCommand, rule_result: RuleEvaluationResult) -> DecisionResult:
        pass
