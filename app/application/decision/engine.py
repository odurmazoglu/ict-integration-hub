from __future__ import annotations

from time import perf_counter

from app.application.commands import ImportInvoiceCommand
from app.application.decision.resolver import WorkflowStrategyResolver
from app.application.dto import DecisionResult
from app.application.ports import RuleEngine


class DecisionEngine:
    """Select and execute one procurement workflow for an imported invoice."""

    def __init__(self, *, rule_engine: RuleEngine, strategy_resolver: WorkflowStrategyResolver) -> None:
        self._rule_engine = rule_engine
        self._strategy_resolver = strategy_resolver

    async def decide(self, command: ImportInvoiceCommand) -> DecisionResult:
        started = perf_counter()
        rule_result = self._rule_engine.evaluate(command)
        strategy = self._strategy_resolver.resolve(rule_result.workflow)
        result = await strategy.execute(command, rule_result)
        return DecisionResult(
            success=result.success,
            invoice_id=result.invoice_id,
            workflow=result.workflow,
            strategy=result.strategy,
            status=result.status,
            vendor_bill_id=result.vendor_bill_id,
            review_required=result.review_required,
            review_reasons=result.review_reasons,
            warnings=rule_result.warnings + result.warnings,
            errors=rule_result.errors + result.errors,
            duration=perf_counter() - started,
        )
