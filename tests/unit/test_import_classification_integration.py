from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.commands import ImportInvoiceCommand
from app.application.decision import DecisionEngine, ManualReviewStrategy, WorkflowStrategyResolver
from app.application.dto import DecisionResult, RuleEvaluationResult
from app.application.rules import (
    InvoiceClassificationContext,
    InvoiceClassificationResult,
    InvoiceClassificationStatus,
    InvoiceDecisionRule,
    InvoiceDecisionRuleAction,
    InvoiceDecisionRuleEngine,
    InvoiceDecisionRuleMatch,
    InvoiceDecisionRulePriority,
    build_invoice_classification_context,
)
from app.application.use_cases import ImportInvoiceUseCase
from app.application.workflow import (
    ManualReviewDecision,
    ManualReviewReason,
    ManualReviewReasonCode,
    WorkflowDecision,
    WorkflowType,
)
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.tax_mapping import InvoiceTaxMappingResult


def test_canonical_context_is_built_from_authoritative_import_evidence() -> None:
    context = build_invoice_classification_context(
        invoice=_invoice(invoice_type="E_INVOICE"),
        company_id=7,
        partner_match=_partner_match(partner_id=501),
        product_match=_product_match(product_id=9001),
    )

    assert context == InvoiceClassificationContext.from_line_descriptions(
        company_id=7,
        vendor_partner_id=501,
        vendor_tax_id="1234567890",
        currency="TRY",
        provider_document_type="E_INVOICE",
        purchase_order_present=None,
        line_descriptions=("Azure Consumption",),
        product_mapping_ids=(9001,),
    )


def test_unavailable_optional_evidence_remains_absent_and_required_rule_does_not_match() -> None:
    context = build_invoice_classification_context(
        invoice=_invoice(invoice_type=None),
        company_id=7,
        partner_match=None,
        product_match=None,
    )
    rule = _rule(match=InvoiceDecisionRuleMatch(vendor_partner_id=501, product_mapping_id=9001))

    result = InvoiceDecisionRuleEngine().classify(context=context, rules=(rule,))

    assert context.vendor_partner_id is None
    assert context.product_mapping_ids == ()
    assert context.purchase_order_present is None
    assert result.status is InvoiceClassificationStatus.NO_MATCH


@pytest.mark.asyncio
async def test_decision_rule_repository_called_with_exact_company_and_engine_reused() -> None:
    repository = FakeDecisionRuleRepository((_rule(),))
    classifier = RecordingClassificationEngine()
    engine = _decision_engine(repository=repository, classifier=classifier)

    result = await engine.decide(_command(company_id=7))

    assert repository.company_calls == [7]
    assert len(classifier.calls) == 1
    assert classifier.calls[0][0].company_id == 7
    assert classifier.calls[0][1] == (_rule(),)
    assert result.classification_result is classifier.result


@pytest.mark.asyncio
async def test_matched_review_required_no_match_and_conflict_results_propagate() -> None:
    for status in (
        InvoiceClassificationStatus.MATCHED,
        InvoiceClassificationStatus.REVIEW_REQUIRED,
        InvoiceClassificationStatus.NO_MATCH,
        InvoiceClassificationStatus.CONFLICT,
    ):
        classification = _classification_result(status)
        engine = _decision_engine(classifier=RecordingClassificationEngine(classification))

        result = await engine.decide(_command(company_id=7))

        assert result.classification_result == classification


@pytest.mark.asyncio
async def test_import_result_carries_classification_evidence_from_decision_path() -> None:
    classification = _classification_result(InvoiceClassificationStatus.MATCHED)
    use_case = ImportInvoiceUseCase(
        import_history=FakeImportHistory(),
        decision_engine=FakeDecisionEngine(_decision_result(classification_result=classification)),
    )

    result = await use_case.execute(_command(company_id=7))

    assert result.classification_result == classification
    assert result.classification_result is not None
    assert result.classification_result.matched_rule_evidence[0].rule_id == "odoo:1"
    assert result.classification_result.classification_code == "CLOUD_COST"
    assert result.classification_result.workflow is WorkflowType.VENDOR_BILL
    assert result.classification_result.require_business_context is True


@pytest.mark.asyncio
async def test_no_match_and_conflict_coexist_with_existing_manual_review_path() -> None:
    manual_result = _manual_review_rule_result()
    for classification in (
        _classification_result(InvoiceClassificationStatus.NO_MATCH),
        _classification_result(InvoiceClassificationStatus.CONFLICT),
    ):
        engine = DecisionEngine(
            rule_engine=FakeRuleEngine(manual_result),
            strategy_resolver=WorkflowStrategyResolver([ManualReviewStrategy()]),
            decision_rule_repository=FakeDecisionRuleRepository((_rule(),)),
            invoice_decision_rule_engine=RecordingClassificationEngine(classification),
        )

        result = await engine.decide(_command(company_id=7))

        assert result.status == "review_required"
        assert result.review_required is True
        assert result.classification_result == classification


@pytest.mark.asyncio
async def test_existing_decision_engine_behavior_is_not_bypassed_by_matched_classification() -> None:
    strategy = RecordingStrategy()
    engine = _decision_engine(
        classifier=RecordingClassificationEngine(_classification_result(InvoiceClassificationStatus.MATCHED)),
        strategy_resolver=WorkflowStrategyResolver([strategy]),
    )
    command = _command(company_id=7)

    await engine.decide(command)

    assert strategy.commands == [command]
    assert strategy.rule_results[0].workflow is WorkflowType.VENDOR_BILL


@pytest.mark.asyncio
async def test_no_erp_writer_runtime_or_customer_invoice_behavior_is_invoked() -> None:
    strategy = RecordingStrategy()
    await _decision_engine(strategy_resolver=WorkflowStrategyResolver([strategy])).decide(_command(company_id=7))

    assert strategy.vendor_bill_writer_calls == []
    assert strategy.customer_invoice_writer_calls == []
    assert strategy.runtime_calls == []


@pytest.mark.asyncio
async def test_identical_orchestration_is_deterministic() -> None:
    engine = _decision_engine()
    command = _command(company_id=7)

    first = await engine.decide(command)
    second = await engine.decide(replace(command))

    assert first.classification_result == second.classification_result


def test_classification_integration_architecture_guards() -> None:
    sources = (
        Path("app/application/rules/classification_context.py").read_text()
        + Path("app/application/decision/engine.py").read_text()
        + Path("app/application/use_cases/import_invoice.py").read_text()
    ).lower()

    assert "odoodecisionrulerepository" not in sources
    assert "app.erp.odoo" not in sources
    assert "app.connectors" not in sources
    assert "uyumsoft" not in sources
    assert "vendorbillwriter" not in sources
    assert "customerinvoicewriter" not in sources
    assert "runtimecoordinator" not in sources
    assert "ai_advisor" not in sources
    assert "openai" not in sources
    assert "fuzzy" not in sources
    assert "history" not in Path("app/application/rules/classification_context.py").read_text().lower()
    assert "classify(" not in Path("app/application/use_cases/import_invoice.py").read_text()


class FakeDecisionRuleRepository:
    def __init__(self, rules: tuple[InvoiceDecisionRule, ...]) -> None:
        self.rules = rules
        self.company_calls: list[int] = []

    def list_invoice_decision_rules(self, *, company_id: int) -> tuple[InvoiceDecisionRule, ...]:
        self.company_calls.append(company_id)
        return self.rules


class RecordingClassificationEngine(InvoiceDecisionRuleEngine):
    def __init__(self, result: InvoiceClassificationResult | None = None) -> None:
        self.result = result or _classification_result(InvoiceClassificationStatus.MATCHED)
        self.calls: list[tuple[InvoiceClassificationContext, tuple[InvoiceDecisionRule, ...]]] = []

    def classify(
        self,
        *,
        context: InvoiceClassificationContext,
        rules: tuple[InvoiceDecisionRule, ...],
    ) -> InvoiceClassificationResult:
        self.calls.append((context, rules))
        return self.result


class FakeRuleEngine:
    def __init__(self, result: RuleEvaluationResult | None = None) -> None:
        self.result = result or _vendor_bill_rule_result()

    def evaluate(self, command: ImportInvoiceCommand) -> RuleEvaluationResult:
        return self.result


class RecordingStrategy:
    workflow = WorkflowType.VENDOR_BILL
    name = WorkflowType.VENDOR_BILL.value

    def __init__(self) -> None:
        self.commands: list[ImportInvoiceCommand] = []
        self.rule_results: list[RuleEvaluationResult] = []
        self.vendor_bill_writer_calls: list[str] = []
        self.customer_invoice_writer_calls: list[str] = []
        self.runtime_calls: list[str] = []

    async def execute(self, command: ImportInvoiceCommand, rule_result: RuleEvaluationResult) -> DecisionResult:
        self.commands.append(command)
        self.rule_results.append(rule_result)
        return _decision_result()


class FakeImportHistory:
    def find_imported_invoice(self, idempotency_key: str) -> None:
        return None


class FakeDecisionEngine:
    def __init__(self, result: DecisionResult) -> None:
        self.result = result

    async def decide(self, command: ImportInvoiceCommand) -> DecisionResult:
        return self.result


def _decision_engine(
    *,
    repository: FakeDecisionRuleRepository | None = None,
    classifier: InvoiceDecisionRuleEngine | None = None,
    strategy_resolver: WorkflowStrategyResolver | None = None,
) -> DecisionEngine:
    return DecisionEngine(
        rule_engine=FakeRuleEngine(),
        strategy_resolver=strategy_resolver or WorkflowStrategyResolver([RecordingStrategy()]),
        decision_rule_repository=repository or FakeDecisionRuleRepository((_rule(),)),
        invoice_decision_rule_engine=classifier or InvoiceDecisionRuleEngine(),
    )


def _command(*, company_id: int | None = 7) -> ImportInvoiceCommand:
    return ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN", company_id=company_id)


def _invoice(*, invoice_type: str | None = "E_INVOICE") -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="INV-1",
            invoice_uuid="INV-UUID",
            ettn="INV-ETTN",
            issue_date=date(2026, 8, 12),
            currency_code="TRY",
            invoice_type=invoice_type,
        ),
        supplier=Party(name="Microsoft", tax_number="1234567890"),
        customer=Party(name="Customer"),
        totals=MonetaryTotals(payable_amount=Decimal("120")),
        lines=(InvoiceLine(line_number="1", description="Azure Consumption"),),
    )


def _rule(
    *,
    match: InvoiceDecisionRuleMatch | None = None,
    action: InvoiceDecisionRuleAction | None = None,
) -> InvoiceDecisionRule:
    return InvoiceDecisionRule(
        rule_id="odoo:1",
        rule_code="RULE-CLOUD",
        rule_version=3,
        name="Cloud Cost",
        enabled=True,
        priority=InvoiceDecisionRulePriority(tier=10, rank=100),
        match=match or InvoiceDecisionRuleMatch(vendor_tax_id="1234567890", currency="TRY"),
        action=action
        or InvoiceDecisionRuleAction(
            workflow=WorkflowType.VENDOR_BILL,
            classification_code="CLOUD_COST",
            require_business_context=True,
        ),
    )


def _classification_result(status: InvoiceClassificationStatus) -> InvoiceClassificationResult:
    rule = _rule()
    if status is InvoiceClassificationStatus.NO_MATCH:
        return InvoiceClassificationResult(status=status)
    if status is InvoiceClassificationStatus.CONFLICT:
        other = _rule(
            action=InvoiceDecisionRuleAction(workflow=WorkflowType.EXPENSE, classification_code="EV_CHARGING")
        )
        return InvoiceClassificationResult(
            status=status,
            matched_rules=(rule, other),
            conflict_rule_evidence=(
                rule_evidence(rule),
                rule_evidence(other),
            ),
        )
    selected_rule = (
        _rule(
            action=InvoiceDecisionRuleAction(
                workflow=WorkflowType.VENDOR_BILL,
                classification_code="CLOUD_COST",
                require_review=True,
                require_business_context=True,
            )
        )
        if status is InvoiceClassificationStatus.REVIEW_REQUIRED
        else rule
    )
    return InvoiceClassificationResult(
        status=status,
        matched_rules=(selected_rule,),
        selected_rule=selected_rule,
        matched_rule_evidence=(rule_evidence(selected_rule),),
    )


def rule_evidence(rule: InvoiceDecisionRule):
    from app.application.rules import InvoiceClassificationRuleEvidence

    return InvoiceClassificationRuleEvidence.from_rule(rule)


def _vendor_bill_rule_result() -> RuleEvaluationResult:
    return RuleEvaluationResult(
        workflow_decision=WorkflowDecision(workflow=WorkflowType.VENDOR_BILL),
        partner_match=_partner_match(partner_id=501),
        product_match=_product_match(product_id=9001),
        tax_match=InvoiceTaxMappingResult(),
    )


def _manual_review_rule_result() -> RuleEvaluationResult:
    return RuleEvaluationResult(
        workflow_decision=WorkflowDecision(
            workflow=WorkflowType.MANUAL_REVIEW,
            manual_review=ManualReviewDecision(summary="Manual review required.", reasons=_review_reasons()),
        ),
        partner_match=_partner_match(partner_id=None, status=PartnerMatchStatus.NOT_FOUND),
        product_match=InvoiceProductMatchResult(),
        tax_match=InvoiceTaxMappingResult(),
    )


def _partner_match(
    *,
    partner_id: int | None,
    status: PartnerMatchStatus = PartnerMatchStatus.MATCHED,
) -> PartnerMatchResult:
    return PartnerMatchResult(
        status=status,
        partner_id=partner_id,
        matched_by="tax_number" if partner_id is not None else None,
        reason="matched" if partner_id is not None else "not found",
        candidate_count=1 if partner_id is not None else 0,
        confidence=Decimal("1.00") if partner_id is not None else None,
    )


def _product_match(*, product_id: int | None) -> InvoiceProductMatchResult:
    status = ProductMatchStatus.MATCHED if product_id is not None else ProductMatchStatus.NOT_FOUND
    return InvoiceProductMatchResult(
        line_results=(
            InvoiceProductLineResult(
                line_number="1",
                result=ProductMatchResult(
                    status=status,
                    line_number="1",
                    product_id=product_id,
                    default_code="AZURE",
                    barcode=None,
                    seller_item_code=None,
                    matched_by="default_code" if product_id is not None else None,
                    reason="matched" if product_id is not None else "not found",
                    candidate_count=1 if product_id is not None else 0,
                    confidence=Decimal("1.00") if product_id is not None else None,
                ),
            ),
        )
    )


def _decision_result(*, classification_result: InvoiceClassificationResult | None = None) -> DecisionResult:
    return DecisionResult(
        success=True,
        invoice_id="INV-ETTN",
        workflow=WorkflowType.VENDOR_BILL,
        strategy=WorkflowType.VENDOR_BILL.value,
        status="dry_run",
        classification_result=classification_result,
    )


def _review_reasons() -> tuple[ManualReviewReason, ...]:
    return (
        ManualReviewReason(
            code=ManualReviewReasonCode.SUPPLIER_NOT_FOUND,
            message="Supplier was not matched deterministically.",
            source="partner_matching",
        ),
    )
