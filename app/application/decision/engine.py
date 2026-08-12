from __future__ import annotations

from time import perf_counter

from app.application.commands import ImportInvoiceCommand
from app.application.decision.resolver import WorkflowStrategyResolver
from app.application.dto import DecisionResult, RuleEvaluationResult
from app.application.ports import DecisionRuleRepository, RuleEngine
from app.application.rules import (
    InvoiceClassificationResult,
    InvoiceDecisionRuleEngine,
    build_invoice_classification_context,
)


class DecisionEngine:
    """Select and execute one procurement workflow for an imported invoice."""

    def __init__(
        self,
        *,
        rule_engine: RuleEngine,
        strategy_resolver: WorkflowStrategyResolver,
        decision_rule_repository: DecisionRuleRepository | None = None,
        invoice_decision_rule_engine: InvoiceDecisionRuleEngine | None = None,
    ) -> None:
        self._rule_engine = rule_engine
        self._strategy_resolver = strategy_resolver
        self._decision_rule_repository = decision_rule_repository
        self._invoice_decision_rule_engine = invoice_decision_rule_engine

    async def decide(self, command: ImportInvoiceCommand) -> DecisionResult:
        started = perf_counter()
        rule_result = self._rule_engine.evaluate(command)
        classification_result = self._classify(command, rule_result)
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
            classification_result=classification_result,
            warnings=rule_result.warnings + result.warnings,
            errors=rule_result.errors + result.errors,
            duration=perf_counter() - started,
        )

    def _classify(
        self,
        command: ImportInvoiceCommand,
        rule_result: RuleEvaluationResult,
    ) -> InvoiceClassificationResult | None:
        if self._decision_rule_repository is None or self._invoice_decision_rule_engine is None:
            return None
        if type(command.company_id) is not int or command.company_id <= 0:
            return None
        context = build_invoice_classification_context(
            invoice=command.invoice,
            company_id=command.company_id,
            partner_match=rule_result.partner_match,
            product_match=rule_result.product_match,
        )
        rules = self._decision_rule_repository.list_invoice_decision_rules(company_id=command.company_id)
        return self._invoice_decision_rule_engine.classify(context=context, rules=rules)
