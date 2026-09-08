from __future__ import annotations

import ast
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands import ImportInvoiceCommand
from app.application.decision import (
    DecisionEngine,
    ManualReviewStrategy,
    VendorBillReviewRecommendationStrategy,
    WorkflowStrategyResolver,
)
from app.application.decision.exceptions import UnsupportedWorkflowError
from app.application.dto import DecisionResult, ImportInvoiceResult, RuleEvaluationResult
from app.application.use_cases import ImportInvoiceUseCase
from app.application.workbench import (
    ReviewClassificationEvidence,
    ReviewDecisionCommand,
    ReviewDecisionType,
    ReviewExecutionEvidence,
    ReviewItem,
    ReviewItemCreationService,
    ReviewSourceInvoiceEvidence,
    ReviewStatus,
    SubmitReviewDecisionUseCase,
)
from app.application.workflow import (
    ManualReviewDecision,
    ManualReviewReason,
    ManualReviewReasonCode,
    WorkflowDecision,
    WorkflowType,
)
from app.db.base import Base
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.models.execution_source_invoice_evidence import ExecutionSourceInvoiceEvidence
from app.models.workbench_review_classification_evidence import WorkbenchReviewClassificationEvidence
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_source_invoice_evidence import WorkbenchReviewSourceInvoiceEvidence
from app.persistence import SqlAlchemyReviewRepository, SqlAlchemyUnitOfWork
from app.persistence.review_execution_evidence_reader import SqlAlchemyReviewExecutionEvidenceReader
from app.tax_mapping import (
    InvoiceTaxLineResult,
    InvoiceTaxMappingResult,
    TaxMatchResult,
    TaxMatchStatus,
    TaxType,
)

COMPANY_ID = 7
ETTN = "INV-ETTN"
IDEMPOTENCY_KEY = "uyumsoft:7:INV-ETTN"


# --------------------------------------------------------------------------- builders


def _invoice() -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="INV-1",
            invoice_uuid="INV-UUID",
            ettn=ETTN,
            issue_date=date(2026, 7, 30),
            currency_code="TRY",
        ),
        supplier=Party(name="Supplier", tax_number="1234567890"),
        customer=Party(name="Customer"),
        totals=MonetaryTotals(payable_amount=Decimal("120")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Line 1",
                buyer_item_code="SKU-1",
                quantity=Decimal("2"),
                unit_code="NIU",
                unit_price=Decimal("50"),
                taxes=(Tax(tax_type="VAT", rate=Decimal("20")),),
            ),
        ),
    )


def _partner_match(status: PartnerMatchStatus = PartnerMatchStatus.MATCHED) -> PartnerMatchResult:
    return PartnerMatchResult(
        status=status,
        partner_id=10 if status is PartnerMatchStatus.MATCHED else None,
        matched_by="tax_number" if status is PartnerMatchStatus.MATCHED else None,
        reason="Unique supplier partner match.",
        candidate_count=1 if status is PartnerMatchStatus.MATCHED else 0,
        confidence=Decimal("1.00") if status is PartnerMatchStatus.MATCHED else None,
    )


def _product_match(status: ProductMatchStatus = ProductMatchStatus.MATCHED) -> InvoiceProductMatchResult:
    product_id = 20 if status is ProductMatchStatus.MATCHED else None
    return InvoiceProductMatchResult(
        line_results=(
            InvoiceProductLineResult(
                line_number="1",
                result=ProductMatchResult(
                    status=status,
                    line_number="1",
                    product_id=product_id,
                    default_code="SKU-1",
                    barcode=None,
                    seller_item_code=None,
                    matched_by="default_code" if product_id is not None else None,
                    reason="Product match result.",
                    candidate_count=1 if product_id is not None else 0,
                    confidence=Decimal("1.00") if product_id is not None else None,
                ),
            ),
        )
    )


def _tax_match(status: TaxMatchStatus = TaxMatchStatus.MATCHED) -> InvoiceTaxMappingResult:
    return InvoiceTaxMappingResult(
        line_results=(
            InvoiceTaxLineResult(
                line_number="1",
                tax_index=0,
                result=TaxMatchResult(
                    status=status,
                    tax_id=30 if status is TaxMatchStatus.MATCHED else None,
                    company_id=COMPANY_ID,
                    tax_type=TaxType.VAT,
                    tax_rate=Decimal("20"),
                    matched_by="company_type_rate" if status is TaxMatchStatus.MATCHED else None,
                    confidence=Decimal("1.00") if status is TaxMatchStatus.MATCHED else None,
                    reason="Exact tax match.",
                    candidate_count=1 if status is TaxMatchStatus.MATCHED else 0,
                ),
            ),
        )
    )


def _matched_rule_result() -> RuleEvaluationResult:
    return RuleEvaluationResult(
        workflow_decision=WorkflowDecision(
            workflow=WorkflowType.VENDOR_BILL,
            matched_rule="vendor_bill_direct_import",
            explanation="Direct vendor bill import.",
            warnings=("rules ok",),
        ),
        partner_match=_partner_match(),
        product_match=_product_match(),
        tax_match=_tax_match(),
        warnings=("rules ok",),
    )


def _manual_review_rule_result() -> RuleEvaluationResult:
    return RuleEvaluationResult(
        workflow_decision=WorkflowDecision(
            workflow=WorkflowType.MANUAL_REVIEW,
            matched_rule="RULE-MANUAL",
            explanation="Deterministic mismatch requires Manual Review.",
            manual_review=ManualReviewDecision(
                reasons=(
                    ManualReviewReason(
                        code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
                        message="Product was not matched deterministically.",
                        source="product_matching",
                        candidate_count=0,
                    ),
                ),
                summary="1 review reason.",
            ),
        ),
        partner_match=_partner_match(),
        product_match=_product_match(ProductMatchStatus.NOT_FOUND),
        tax_match=_tax_match(),
    )


def _command() -> ImportInvoiceCommand:
    return ImportInvoiceCommand(invoice=_invoice(), idempotency_key=IDEMPOTENCY_KEY, company_id=COMPANY_ID)


class FakeRuleEngine:
    def __init__(self, rule_result: RuleEvaluationResult) -> None:
        self._rule_result = rule_result

    def evaluate(self, command: ImportInvoiceCommand) -> RuleEvaluationResult:
        return self._rule_result


class FakeImportHistory:
    def find_imported_invoice(self, idempotency_key: str):
        return None


class RecordingReviewItemCreationService:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.execution_evidence: ReviewExecutionEvidence | None = None
        self.classification_evidence: ReviewClassificationEvidence | None = None
        self.source_invoice_evidence: ReviewSourceInvoiceEvidence | None = None
        self.created_item: ReviewItem | None = None

    def create_pending_review_item(
        self, item: ReviewItem, *, company_id: int, idempotency_key: str, source_invoice_evidence=None
    ) -> ReviewItem:
        self.calls.append("plain")
        self.created_item = item
        self.source_invoice_evidence = source_invoice_evidence
        return item

    def create_pending_review_item_with_classification_evidence(
        self, item, *, company_id, idempotency_key, classification_evidence, source_invoice_evidence=None
    ) -> ReviewItem:
        self.calls.append("classification")
        self.created_item = item
        self.classification_evidence = classification_evidence
        self.source_invoice_evidence = source_invoice_evidence
        return item

    def create_pending_review_item_with_execution_evidence(
        self, item, *, company_id, idempotency_key, evidence, classification_evidence=None, source_invoice_evidence=None
    ) -> ReviewItem:
        self.calls.append("execution")
        self.created_item = item
        self.execution_evidence = evidence
        self.classification_evidence = classification_evidence
        self.source_invoice_evidence = source_invoice_evidence
        return item


class RecordingUnitOfWork:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:  # pragma: no cover
        raise AssertionError("no rollback expected")


def _decision_engine(rule_result: RuleEvaluationResult) -> DecisionEngine:
    return DecisionEngine(
        rule_engine=FakeRuleEngine(rule_result),
        strategy_resolver=WorkflowStrategyResolver([VendorBillReviewRecommendationStrategy(), ManualReviewStrategy()]),
    )


# --------------------------------------------------------------------------- A. DecisionResult


def test_decision_result_carries_typed_match_dtos() -> None:
    partner = _partner_match()
    product = _product_match()
    tax = _tax_match()
    result = DecisionResult(
        success=True,
        invoice_id=ETTN,
        workflow=WorkflowType.VENDOR_BILL,
        strategy="s",
        status="review_required",
        partner_match=partner,
        product_match=product,
        tax_match=tax,
    )
    assert result.partner_match is partner
    assert result.product_match is product
    assert result.tax_match is tax


def test_decision_result_match_fields_default_to_none() -> None:
    result = DecisionResult(
        success=True, invoice_id=ETTN, workflow=WorkflowType.MANUAL_REVIEW, strategy="s", status="review_required"
    )
    assert (result.partner_match, result.product_match, result.tax_match) == (None, None, None)


# --------------------------------------------------------------------------- B. DecisionEngine propagation


async def test_decision_engine_propagates_exact_match_dtos() -> None:
    rule_result = _matched_rule_result()
    result = await _decision_engine(rule_result).decide(_command())

    assert result.partner_match is rule_result.partner_match
    assert result.product_match is rule_result.product_match
    assert result.tax_match is rule_result.tax_match


# --------------------------------------------------------------------------- C. recommendation strategy


async def test_recommendation_strategy_returns_review_required_without_writer() -> None:
    rule_result = _matched_rule_result()
    result = await VendorBillReviewRecommendationStrategy().execute(_command(), rule_result)

    assert result.success is True
    assert result.review_required is True
    assert result.workflow is WorkflowType.VENDOR_BILL
    assert result.status == "review_required"
    assert result.vendor_bill_id is None
    assert result.partner_match is rule_result.partner_match
    assert result.product_match is rule_result.product_match
    assert result.tax_match is rule_result.tax_match


def test_recommendation_strategy_has_no_builder_or_writer_dependency() -> None:
    strategy = VendorBillReviewRecommendationStrategy()
    assert not hasattr(strategy, "_vendor_bill_builder")
    assert not hasattr(strategy, "_vendor_bill_writer")

    tree = ast.parse(Path("app/application/decision/vendor_bill_review_strategy.py").read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    assert not any(module.startswith(("app.erp", "app.billing", "app.connectors")) for module in modules)

    body = list(tree.body)
    if body and isinstance(body[0], ast.Expr):
        body = body[1:]
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            node.value.value = ""
    code = ast.unparse(ast.Module(body=body, type_ignores=[]))
    for token in ("VendorBillBuilder", "write_vendor_bill", "OdooVendorBillWriter", "account.move", "action_post"):
        assert token not in code


async def test_recommendation_strategy_rejects_wrong_workflow_and_missing_matches() -> None:
    with pytest.raises(UnsupportedWorkflowError):
        await VendorBillReviewRecommendationStrategy().execute(_command(), _manual_review_rule_result())
    bare = RuleEvaluationResult(workflow_decision=WorkflowDecision(WorkflowType.VENDOR_BILL))
    with pytest.raises(UnsupportedWorkflowError):
        await VendorBillReviewRecommendationStrategy().execute(_command(), bare)


# --------------------------------------------------------------------------- D. import composition


def test_import_composition_registers_recommendation_strategy_only() -> None:
    source = Path("app/composition/imports.py").read_text(encoding="utf-8")
    assert "VendorBillReviewRecommendationStrategy()" in source
    assert "VendorBillStrategy(" not in source
    assert "OdooVendorBillWriter(" not in source
    assert "VendorBillBuilder()" not in source
    assert "AccountMoveRepository(" not in source


# --------------------------------------------------------------------------- E. ImportInvoiceUseCase (recording)


def _import_use_case(
    *, rule_result: RuleEvaluationResult, service: RecordingReviewItemCreationService
) -> ImportInvoiceUseCase:
    return ImportInvoiceUseCase(
        import_history=FakeImportHistory(),
        decision_engine=_decision_engine(rule_result),
        review_item_creation_service=service,
        unit_of_work=RecordingUnitOfWork(),
    )


async def test_matched_import_creates_vendor_bill_review_with_stage1_evidence() -> None:
    service = RecordingReviewItemCreationService()
    result = await _import_use_case(rule_result=_matched_rule_result(), service=service).execute(_command())

    assert isinstance(result, ImportInvoiceResult)
    assert result.review_required is True
    assert service.calls == ["execution"]
    assert service.created_item is not None
    assert service.created_item.workflow is WorkflowType.VENDOR_BILL
    assert service.created_item.status is ReviewStatus.PENDING_REVIEW

    evidence = service.execution_evidence
    assert isinstance(evidence, ReviewExecutionEvidence)
    assert evidence.review_id == service.created_item.review_id
    assert evidence.company_id == COMPANY_ID
    assert evidence.review_version == service.created_item.version == 1
    assert evidence.source_invoice_id == ETTN
    assert evidence.invoice == _invoice()
    assert evidence.partner_match == _partner_match()
    assert evidence.product_match == _product_match()
    assert evidence.tax_match == _tax_match()


async def test_matched_import_persists_classification_evidence_atomically_when_present() -> None:
    service = RecordingReviewItemCreationService()
    rule_result = _matched_rule_result()
    engine = _decision_engine(rule_result)
    use_case = ImportInvoiceUseCase(
        import_history=FakeImportHistory(),
        decision_engine=engine,
        review_item_creation_service=service,
        unit_of_work=RecordingUnitOfWork(),
    )

    # Force a classification result onto the decision output via a patched engine.
    original_decide = engine.decide

    async def decide_with_classification(command):
        base = await original_decide(command)
        from dataclasses import replace

        return replace(base, classification_result=_classification_result())

    engine.decide = decide_with_classification  # type: ignore[method-assign]

    await use_case.execute(_command())

    assert service.calls == ["execution"]
    assert service.execution_evidence is not None
    assert service.classification_evidence is not None
    assert service.classification_evidence.review_id == service.created_item.review_id  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "rule_result_factory",
    (
        lambda: RuleEvaluationResult(
            workflow_decision=WorkflowDecision(WorkflowType.VENDOR_BILL, matched_rule="r", explanation="e"),
            partner_match=_partner_match(PartnerMatchStatus.NOT_FOUND),
            product_match=_product_match(),
            tax_match=_tax_match(),
        ),
        lambda: RuleEvaluationResult(
            workflow_decision=WorkflowDecision(WorkflowType.VENDOR_BILL, matched_rule="r", explanation="e"),
            partner_match=_partner_match(),
            product_match=_product_match(ProductMatchStatus.NOT_FOUND),
            tax_match=_tax_match(),
        ),
        lambda: RuleEvaluationResult(
            workflow_decision=WorkflowDecision(WorkflowType.VENDOR_BILL, matched_rule="r", explanation="e"),
            partner_match=_partner_match(),
            product_match=_product_match(),
            tax_match=_tax_match(TaxMatchStatus.NOT_FOUND),
        ),
    ),
)
async def test_incomplete_match_vendor_bill_recommendation_gets_no_execution_evidence(rule_result_factory) -> None:
    # The recommendation strategy still runs (workflow is VENDOR_BILL) but the
    # canonical completeness check fails, so no Stage-1 evidence is produced.
    service = RecordingReviewItemCreationService()
    await _import_use_case(rule_result=rule_result_factory(), service=service).execute(_command())

    assert service.execution_evidence is None
    assert service.created_item is not None  # a normal review item is still created
    assert "execution" not in service.calls


async def test_manual_review_import_still_creates_review_item_without_execution_evidence() -> None:
    service = RecordingReviewItemCreationService()
    result = await _import_use_case(rule_result=_manual_review_rule_result(), service=service).execute(_command())

    assert result.review_required is True
    assert service.execution_evidence is None
    assert service.created_item is not None
    assert service.created_item.workflow is WorkflowType.MANUAL_REVIEW


# ------------------------------------------------- F. real repository: idempotency + Stage-1 -> Stage-2


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewExecutionEvidence.__table__,
            WorkbenchReviewClassificationEvidence.__table__,
            WorkbenchReviewDecision.__table__,
            ExecutionSourceInvoiceEvidence.__table__,
            WorkbenchReviewSourceInvoiceEvidence.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


def _real_import_use_case(session: Session, rule_result: RuleEvaluationResult) -> ImportInvoiceUseCase:
    return ImportInvoiceUseCase(
        import_history=FakeImportHistory(),
        decision_engine=_decision_engine(rule_result),
        review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(session)),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


async def test_matched_import_persists_review_and_stage1_evidence_in_repository(session: Session) -> None:
    await _real_import_use_case(session, _matched_rule_result()).execute(_command())

    item = session.scalar(select(WorkbenchReviewItem))
    assert item is not None
    assert item.workflow == WorkflowType.VENDOR_BILL.value
    evidence = session.scalar(select(WorkbenchReviewExecutionEvidence))
    assert evidence is not None
    assert evidence.review_id == item.review_id
    assert evidence.company_id == COMPANY_ID
    assert evidence.review_version == 1
    assert evidence.source_invoice_id == ETTN


async def test_matched_import_replay_is_idempotent(session: Session) -> None:
    use_case = _real_import_use_case(session, _matched_rule_result())
    await use_case.execute(_command())
    await _real_import_use_case(session, _matched_rule_result()).execute(_command())

    assert session.query(WorkbenchReviewItem).count() == 1
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 1


async def test_conflicting_stage1_evidence_fails_closed(session: Session) -> None:
    await _real_import_use_case(session, _matched_rule_result()).execute(_command())

    conflicting = RuleEvaluationResult(
        workflow_decision=WorkflowDecision(WorkflowType.VENDOR_BILL, matched_rule="r", explanation="e", warnings=()),
        partner_match=PartnerMatchResult(
            status=PartnerMatchStatus.MATCHED,
            partner_id=999,
            matched_by="tax_number",
            reason="Different supplier partner.",
            candidate_count=1,
            confidence=Decimal("1.00"),
        ),
        product_match=_product_match(),
        tax_match=_tax_match(),
    )
    with pytest.raises(Exception):  # noqa: B017 - repository raises a safe conflict/integrity error
        await _real_import_use_case(session, conflicting).execute(_command())

    assert session.query(WorkbenchReviewExecutionEvidence).count() == 1


async def test_stage1_evidence_enables_accepted_vendor_bill_decision_and_stage2(session: Session) -> None:
    await _real_import_use_case(session, _matched_rule_result()).execute(_command())
    item = session.scalar(select(WorkbenchReviewItem))
    assert item is not None

    submit = SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
    )
    acknowledgement = submit.execute(
        ReviewDecisionCommand(
            review_id=item.review_id,
            company_id=COMPANY_ID,
            expected_version=1,
            decision=ReviewDecisionType.SELECT_WORKFLOW,
            selected_workflow=WorkflowType.VENDOR_BILL,
            decided_by="finance.user",
            idempotency_key="decision:INV-ETTN",
        )
    )
    assert acknowledgement.accepted is True
    assert acknowledgement.version == 2

    stage1 = session.scalar(select(WorkbenchReviewExecutionEvidence))
    stage2 = session.scalar(select(ExecutionSourceInvoiceEvidence))
    assert stage2 is not None
    assert stage2.review_id == stage1.review_id == item.review_id
    assert stage2.company_id == COMPANY_ID
    assert stage2.decision_version == 2
    assert stage2.source_invoice_id == stage1.source_invoice_id == ETTN
    # Stage-2 is a verbatim pin of the immutable Stage-1 source snapshot.
    assert stage2.invoice == stage1.invoice
    assert stage2.partner_match == stage1.partner_match
    assert stage2.product_match == stage1.product_match
    assert stage2.tax_match == stage1.tax_match


# --------------------------------------------------------------------------- helpers


def _classification_result():
    from app.application.rules import (
        InvoiceClassificationResult,
        InvoiceClassificationRuleEvidence,
        InvoiceClassificationStatus,
    )
    from app.application.rules.contracts import (
        InvoiceDecisionRule,
        InvoiceDecisionRuleAction,
        InvoiceDecisionRuleMatch,
        InvoiceDecisionRulePriority,
    )

    rule = InvoiceDecisionRule(
        rule_id="odoo:1",
        rule_code="RULE-CLOUD",
        rule_version=3,
        name="Cloud Cost",
        enabled=True,
        priority=InvoiceDecisionRulePriority(tier=10),
        match=InvoiceDecisionRuleMatch(vendor_tax_id="1234567890"),
        action=InvoiceDecisionRuleAction(
            workflow=WorkflowType.VENDOR_BILL,
            classification_code="CLOUD_COST",
            require_review=True,
            require_business_context=False,
        ),
    )
    return InvoiceClassificationResult(
        status=InvoiceClassificationStatus.MATCHED,
        matched_rules=(rule,),
        selected_rule=rule,
        matched_rule_evidence=(InvoiceClassificationRuleEvidence.from_rule(rule),),
    )
