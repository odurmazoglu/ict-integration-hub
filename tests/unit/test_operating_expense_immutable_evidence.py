"""Immutable operating-expense execution evidence (P0-3C5 / PR 4).

Carries a deterministically matched expense account from Stage-1 review evidence
into Stage-2 execution-source evidence, verbatim. No re-classification, no mapping
query, no Odoo lookup during execution.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.application.commands import ImportInvoiceCommand
from app.application.decision import (
    DecisionEngine,
    ManualReviewStrategy,
    VendorBillReviewRecommendationStrategy,
    WorkflowStrategyResolver,
)
from app.application.dto import RuleEvaluationResult
from app.application.execution.contracts import ExecutionSourceInvoice
from app.application.execution.exceptions import ExecutionPlanningError
from app.application.expense_mapping import (
    OperatingExpenseMatchResult,
    OperatingExpenseMatchStatus,
)
from app.application.use_cases import ImportInvoiceUseCase
from app.application.workbench import (
    ReviewDecisionCommand,
    ReviewDecisionType,
    ReviewExecutionEvidence,
    ReviewItemCreationService,
    SubmitReviewDecisionUseCase,
)
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workflow import WorkflowDecision, WorkflowType
from app.core.config import get_settings
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
from app.persistence import SqlAlchemyExecutionSourceInvoiceReader, SqlAlchemyReviewRepository, SqlAlchemyUnitOfWork
from app.persistence.execution_source_invoice_reader import (
    deserialize_execution_source_invoice_payload,
    serialize_execution_source_invoice_payload,
)
from app.persistence.review_execution_evidence_reader import SqlAlchemyReviewExecutionEvidenceReader
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType

COMPANY_ID = 7
PARTNER_ID = 1001
EXPENSE_ACCOUNT_ID = 9001
PINNED_CATEGORY = "OFFICE_BUILDING_EXPENSE"
ETTN = "OPEX-ETTN"
IDEMPOTENCY_KEY = "uyumsoft:7:OPEX-ETTN"
TAX_ID = 401


# --------------------------------------------------------------------------- builders


def _line(line_number: str = "1", *, buyer_item_code: str | None = None) -> InvoiceLine:
    return InvoiceLine(
        line_number=line_number,
        description=f"Common area fee {line_number}",
        buyer_item_code=buyer_item_code,
        seller_item_code=None,
        barcode=None,
        quantity=Decimal("1"),
        unit_code="C62",
        unit_price=Decimal("83.33"),
        taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
    )


def _invoice(lines: list[InvoiceLine] | None = None) -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="AKM-1",
            invoice_uuid=ETTN,
            ettn=ETTN,
            issue_date=date(2026, 8, 18),
            currency_code="TRY",
        ),
        supplier=Party(name="Akyasam", tax_number="0430367181"),
        customer=Party(name="ICT", tax_number="4651205941"),
        totals=MonetaryTotals(payable_amount=Decimal("100.00")),
        lines=tuple(lines if lines is not None else [_line("1")]),
    )


def _partner(
    status: PartnerMatchStatus = PartnerMatchStatus.MATCHED, *, partner_id: int = PARTNER_ID
) -> PartnerMatchResult:
    matched = status is PartnerMatchStatus.MATCHED
    return PartnerMatchResult(
        status=status,
        partner_id=partner_id if matched else None,
        matched_by="tax_number" if matched else None,
        reason="matched" if matched else "unmatched",
        candidate_count=1 if matched else 0,
        confidence=Decimal("1.00") if matched else None,
    )


def _identifier_free_products(invoice: InternalInvoice) -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(
        line_results=tuple(
            InvoiceProductLineResult(
                line_number=line.line_number,
                result=ProductMatchResult(
                    status=ProductMatchStatus.INVALID_INPUT,
                    line_number=line.line_number,
                    product_id=None,
                    default_code=None,
                    barcode=None,
                    seller_item_code=None,
                    matched_by=None,
                    reason="At least one deterministic product identifier is required.",
                    candidate_count=0,
                    confidence=None,
                ),
            )
            for line in invoice.lines
        )
    )


def _matched_products(invoice: InternalInvoice) -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(
        line_results=tuple(
            InvoiceProductLineResult(
                line_number=line.line_number,
                result=ProductMatchResult(
                    status=ProductMatchStatus.MATCHED,
                    line_number=line.line_number,
                    product_id=2001,
                    default_code="SKU-1",
                    barcode=None,
                    seller_item_code=None,
                    matched_by="default_code",
                    reason="matched",
                    candidate_count=1,
                    confidence=Decimal("1.00"),
                ),
            )
            for line in invoice.lines
        )
    )


def _not_found_products(invoice: InternalInvoice) -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(
        line_results=tuple(
            InvoiceProductLineResult(
                line_number=line.line_number,
                result=ProductMatchResult(
                    status=ProductMatchStatus.NOT_FOUND,
                    line_number=line.line_number,
                    product_id=None,
                    default_code="SKU-1",
                    barcode=None,
                    seller_item_code=None,
                    matched_by=None,
                    reason="not found",
                    candidate_count=0,
                    confidence=None,
                ),
            )
            for line in invoice.lines
        )
    )


def _taxes(invoice: InternalInvoice, status: TaxMatchStatus = TaxMatchStatus.MATCHED) -> InvoiceTaxMappingResult:
    matched = status is TaxMatchStatus.MATCHED
    return InvoiceTaxMappingResult(
        line_results=tuple(
            InvoiceTaxLineResult(
                line_number=line.line_number,
                tax_index=tax_index,
                result=TaxMatchResult(
                    status=status,
                    tax_id=TAX_ID if matched else None,
                    company_id=COMPANY_ID if matched else None,
                    tax_type=TaxType.VAT,
                    tax_rate=Decimal("20"),
                    matched_by="company_type_rate" if matched else None,
                    confidence=Decimal("1.00") if matched else None,
                    reason="matched" if matched else "unmatched",
                    candidate_count=1 if matched else 2,
                ),
            )
            for line in invoice.lines
            for tax_index, _tax in enumerate(line.taxes)
        )
    )


def _expense_match(
    *,
    company_id: int = COMPANY_ID,
    vendor_partner_id: int = PARTNER_ID,
    expense_account_id: int = EXPENSE_ACCOUNT_ID,
    status: OperatingExpenseMatchStatus = OperatingExpenseMatchStatus.MATCHED,
) -> OperatingExpenseMatchResult:
    if status is not OperatingExpenseMatchStatus.MATCHED:
        return OperatingExpenseMatchResult(status=status, reason="not matched", candidate_count=0)
    return OperatingExpenseMatchResult(
        status=status,
        reason="Exact company and supplier partner operating-expense mapping.",
        candidate_count=1,
        mapping_id=42,
        company_id=company_id,
        vendor_partner_id=vendor_partner_id,
        expense_account_id=expense_account_id,
        expense_category=PINNED_CATEGORY,
        matched_by="company_partner",
        confidence=Decimal("1.00"),
    )


def _review_evidence(invoice: InternalInvoice, **overrides) -> ReviewExecutionEvidence:
    base = {
        "review_id": "review-1",
        "company_id": COMPANY_ID,
        "review_version": 1,
        "source_invoice_id": ETTN,
        "invoice": invoice,
        "partner_match": _partner(),
        "product_match": _identifier_free_products(invoice),
        "tax_match": _taxes(invoice),
        "operating_expense_match": _expense_match(),
    }
    base.update(overrides)
    return ReviewExecutionEvidence(**base)


def _source(invoice: InternalInvoice, **overrides) -> ExecutionSourceInvoice:
    base = {
        "review_id": "review-1",
        "company_id": COMPANY_ID,
        "decision_version": 2,
        "source_invoice_id": ETTN,
        "invoice": invoice,
        "partner_match": _partner(),
        "product_match": _identifier_free_products(invoice),
        "tax_match": _taxes(invoice),
        "operating_expense_match": _expense_match(),
    }
    base.update(overrides)
    return ExecutionSourceInvoice(**base)


# --------------------------------------------------------------------------- DTO invariants (1-9)


def test_product_review_evidence_still_valid_without_operating_expense_match() -> None:
    invoice = _invoice([_line("1", buyer_item_code="SKU-1")])
    evidence = _review_evidence(
        invoice,
        product_match=_matched_products(invoice),
        operating_expense_match=None,
    )
    assert evidence.operating_expense_match is None


def test_product_execution_source_still_valid_without_operating_expense_match() -> None:
    invoice = _invoice([_line("1", buyer_item_code="SKU-1")])
    source = _source(invoice, product_match=_matched_products(invoice), operating_expense_match=None)
    assert source.operating_expense_match is None


def test_expense_review_evidence_accepts_exact_matched_result() -> None:
    invoice = _invoice()
    evidence = _review_evidence(invoice)
    assert evidence.operating_expense_match.expense_account_id == EXPENSE_ACCOUNT_ID


def test_expense_execution_source_accepts_exact_matched_result() -> None:
    invoice = _invoice()
    source = _source(invoice)
    assert source.operating_expense_match.expense_account_id == EXPENSE_ACCOUNT_ID


@pytest.mark.parametrize("dto_factory", [_review_evidence, _source])
def test_evidence_rejects_company_mismatch(dto_factory) -> None:
    invoice = _invoice()
    with pytest.raises((WorkbenchContractError, ExecutionPlanningError)):
        dto_factory(invoice, operating_expense_match=_expense_match(company_id=999))


@pytest.mark.parametrize("dto_factory", [_review_evidence, _source])
def test_evidence_rejects_partner_mismatch(dto_factory) -> None:
    invoice = _invoice()
    with pytest.raises((WorkbenchContractError, ExecutionPlanningError)):
        dto_factory(invoice, operating_expense_match=_expense_match(vendor_partner_id=222))


@pytest.mark.parametrize("dto_factory", [_review_evidence, _source])
def test_evidence_rejects_non_positive_expense_account(dto_factory) -> None:
    invoice = _invoice()
    with pytest.raises((WorkbenchContractError, ExecutionPlanningError)):
        dto_factory(invoice, operating_expense_match=_expense_match(expense_account_id=0))


@pytest.mark.parametrize("dto_factory", [_review_evidence, _source])
def test_evidence_rejects_expense_match_when_product_identifier_failed(dto_factory) -> None:
    invoice = _invoice([_line("1", buyer_item_code="SKU-1")])
    with pytest.raises((WorkbenchContractError, ExecutionPlanningError)):
        dto_factory(invoice, product_match=_not_found_products(invoice))


@pytest.mark.parametrize("dto_factory", [_review_evidence, _source])
def test_evidence_rejects_matched_product_result_shape_with_expense_match(dto_factory) -> None:
    invoice = _invoice()
    with pytest.raises((WorkbenchContractError, ExecutionPlanningError)):
        dto_factory(invoice, product_match=_matched_products(invoice))


@pytest.mark.parametrize("dto_factory", [_review_evidence, _source])
def test_evidence_rejects_non_matched_expense_status(dto_factory) -> None:
    invoice = _invoice()
    with pytest.raises((WorkbenchContractError, ExecutionPlanningError)):
        dto_factory(invoice, operating_expense_match=_expense_match(status=OperatingExpenseMatchStatus.NOT_FOUND))


# --------------------------------------------------------------------------- serialization round-trip (10)


def test_operating_expense_match_serialization_round_trip_is_exact() -> None:
    invoice = _invoice()
    source = _source(invoice)

    payload = serialize_execution_source_invoice_payload(source)
    assert payload["operating_expense_match"] == {
        "status": "MATCHED",
        "reason": "Exact company and supplier partner operating-expense mapping.",
        "candidate_count": 1,
        "mapping_id": 42,
        "company_id": COMPANY_ID,
        "vendor_partner_id": PARTNER_ID,
        "expense_account_id": EXPENSE_ACCOUNT_ID,
        "expense_category": PINNED_CATEGORY,
        "matched_by": "company_partner",
        "confidence": "1.00",
    }
    hydrated = deserialize_execution_source_invoice_payload(payload)
    assert hydrated.operating_expense_match == source.operating_expense_match
    assert hydrated.operating_expense_match.confidence == Decimal("1.00")


def test_product_source_serializes_operating_expense_match_as_null() -> None:
    invoice = _invoice([_line("1", buyer_item_code="SKU-1")])
    source = _source(invoice, product_match=_matched_products(invoice), operating_expense_match=None)

    payload = serialize_execution_source_invoice_payload(source)
    assert payload["operating_expense_match"] is None
    assert deserialize_execution_source_invoice_payload(payload).operating_expense_match is None


# --------------------------------------------------------------------------- DB persistence (11-16)


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
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


class _FakeImportHistory:
    def find_imported_invoice(self, idempotency_key: str) -> None:
        return None

    def record_import_result(self, *, company_id: int, idempotency_key: str, result) -> None:
        return None


def _rule_result(invoice: InternalInvoice, *, expense: bool, product_matched: bool = False) -> RuleEvaluationResult:
    return RuleEvaluationResult(
        workflow_decision=WorkflowDecision(
            workflow=WorkflowType.VENDOR_BILL,
            matched_rule="RULE-OPERATING-EXPENSE-VENDOR-BILL-001" if expense else "RULE-DIRECT-VENDOR-BILL-001",
            explanation="deterministic",
        ),
        partner_match=_partner(),
        product_match=_matched_products(invoice) if product_matched else _identifier_free_products(invoice),
        tax_match=_taxes(invoice),
        operating_expense_match=_expense_match() if expense else None,
    )


def _import_use_case(session: Session, rule_result: RuleEvaluationResult) -> ImportInvoiceUseCase:
    engine = DecisionEngine(
        rule_engine=_StubRuleEngine(rule_result),
        strategy_resolver=WorkflowStrategyResolver([VendorBillReviewRecommendationStrategy(), ManualReviewStrategy()]),
    )
    return ImportInvoiceUseCase(
        import_history=_FakeImportHistory(),
        decision_engine=engine,
        review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(session)),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


class _StubRuleEngine:
    def __init__(self, result: RuleEvaluationResult) -> None:
        self._result = result

    def evaluate(self, command: ImportInvoiceCommand) -> RuleEvaluationResult:
        return self._result


def _command(invoice: InternalInvoice) -> ImportInvoiceCommand:
    return ImportInvoiceCommand(invoice=invoice, idempotency_key=IDEMPOTENCY_KEY, company_id=COMPANY_ID)


async def test_stage1_persists_operating_expense_json_snapshot(session: Session) -> None:
    invoice = _invoice()
    await _import_use_case(session, _rule_result(invoice, expense=True)).execute(_command(invoice))

    record = session.scalar(select(WorkbenchReviewExecutionEvidence))
    assert record is not None
    assert record.operating_expense_match["status"] == "MATCHED"
    assert record.operating_expense_match["expense_account_id"] == EXPENSE_ACCOUNT_ID
    assert record.operating_expense_match["confidence"] == "1.00"


async def test_stage1_read_back_reconstructs_typed_operating_expense_match(session: Session) -> None:
    invoice = _invoice()
    await _import_use_case(session, _rule_result(invoice, expense=True)).execute(_command(invoice))

    reader = SqlAlchemyReviewExecutionEvidenceReader(session)
    source = reader.get_evidence(review_id=_only_review_id(session), company_id=COMPANY_ID, expected_version=1)
    assert isinstance(source.operating_expense_match, OperatingExpenseMatchResult)
    assert source.operating_expense_match.expense_account_id == EXPENSE_ACCOUNT_ID
    assert source.operating_expense_match.confidence == Decimal("1.00")


async def test_stage1_row_with_null_operating_expense_still_reads(session: Session) -> None:
    invoice = _invoice([_line("1", buyer_item_code="SKU-1")])
    await _import_use_case(session, _rule_result(invoice, expense=False, product_matched=True)).execute(
        _command(invoice)
    )

    record = session.scalar(select(WorkbenchReviewExecutionEvidence))
    assert record is not None
    assert record.operating_expense_match is None
    reader = SqlAlchemyReviewExecutionEvidenceReader(session)
    source = reader.get_evidence(review_id=record.review_id, company_id=COMPANY_ID, expected_version=1)
    assert source.operating_expense_match is None


def _only_review_id(session: Session) -> str:
    record = session.scalar(select(WorkbenchReviewExecutionEvidence))
    assert record is not None
    return record.review_id


# --------------------------------------------------------------------------- _execution_evidence mode selection (21-24)


async def test_product_vendor_bill_stage1_evidence_has_no_operating_expense_match(session: Session) -> None:
    invoice = _invoice([_line("1", buyer_item_code="SKU-1")])
    await _import_use_case(session, _rule_result(invoice, expense=False, product_matched=True)).execute(
        _command(invoice)
    )

    record = session.scalar(select(WorkbenchReviewExecutionEvidence))
    assert record is not None
    assert record.operating_expense_match is None


async def test_identifier_free_expense_match_creates_stage1_execution_evidence(session: Session) -> None:
    invoice = _invoice()
    await _import_use_case(session, _rule_result(invoice, expense=True)).execute(_command(invoice))

    assert session.scalar(select(WorkbenchReviewExecutionEvidence)) is not None


async def test_identifier_free_without_mapping_creates_no_stage1_execution_evidence(session: Session) -> None:
    invoice = _invoice()
    rule_result = _rule_result(invoice, expense=False)  # identifier-free products, no expense match
    await _import_use_case(session, rule_result).execute(_command(invoice))

    assert session.scalar(select(WorkbenchReviewExecutionEvidence)) is None
    assert session.scalar(select(WorkbenchReviewItem)) is not None


async def test_identifier_present_failed_product_with_expense_match_creates_no_stage1_evidence(
    session: Session,
) -> None:
    invoice = _invoice([_line("1", buyer_item_code="SKU-1")])
    rule_result = RuleEvaluationResult(
        workflow_decision=WorkflowDecision(workflow=WorkflowType.VENDOR_BILL, matched_rule="r", explanation="e"),
        partner_match=_partner(),
        product_match=_not_found_products(invoice),
        tax_match=_taxes(invoice),
        operating_expense_match=_expense_match(),
    )
    await _import_use_case(session, rule_result).execute(_command(invoice))

    assert session.scalar(select(WorkbenchReviewExecutionEvidence)) is None


# --------------------------------------------------------------------------- Stage-1 -> Stage-2 copy (25) + pin (30-31)


def _accept_vendor_bill(session: Session, review_id: str) -> None:
    submit = SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
    )
    ack = submit.execute(
        ReviewDecisionCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=1,
            decision=ReviewDecisionType.SELECT_WORKFLOW,
            selected_workflow=WorkflowType.VENDOR_BILL,
            decided_by="finance.user",
            idempotency_key="decision:OPEX",
        )
    )
    assert ack.accepted is True
    assert ack.version == 2


async def test_stage1_to_stage2_copies_operating_expense_match_verbatim(session: Session) -> None:
    invoice = _invoice()
    await _import_use_case(session, _rule_result(invoice, expense=True)).execute(_command(invoice))
    review_id = _only_review_id(session)

    _accept_vendor_bill(session, review_id)

    stage1 = session.scalar(select(WorkbenchReviewExecutionEvidence))
    stage2 = session.scalar(select(ExecutionSourceInvoiceEvidence))
    assert stage2 is not None
    assert stage2.operating_expense_match == stage1.operating_expense_match
    assert stage2.operating_expense_match["expense_account_id"] == EXPENSE_ACCOUNT_ID


async def test_stage2_reader_reconstructs_pinned_expense_account_without_mapping_repository(session: Session) -> None:
    invoice = _invoice()
    await _import_use_case(session, _rule_result(invoice, expense=True)).execute(_command(invoice))
    review_id = _only_review_id(session)
    _accept_vendor_bill(session, review_id)

    # No OperatingExpenseMappingRepository is constructed here at all.
    reader = SqlAlchemyExecutionSourceInvoiceReader(session)
    source = reader.get_source_invoice(review_id=review_id, company_id=COMPANY_ID, decision_version=2)
    assert isinstance(source.operating_expense_match, OperatingExpenseMatchResult)
    assert source.operating_expense_match.expense_account_id == EXPENSE_ACCOUNT_ID
    assert source.operating_expense_match.mapping_id == 42


async def test_stage2_row_with_null_operating_expense_still_reads(session: Session) -> None:
    invoice = _invoice([_line("1", buyer_item_code="SKU-1")])
    await _import_use_case(session, _rule_result(invoice, expense=False, product_matched=True)).execute(
        _command(invoice)
    )
    review_id = _only_review_id(session)
    _accept_vendor_bill(session, review_id)

    reader = SqlAlchemyExecutionSourceInvoiceReader(session)
    source = reader.get_source_invoice(review_id=review_id, company_id=COMPANY_ID, decision_version=2)
    assert source.operating_expense_match is None


# --------------------------------------------------------------------------- execution strategy + payload (26-29)


def test_execution_strategy_passes_operating_expense_match_to_builder() -> None:
    from app.application.execution import VendorBillExecutionStrategy
    from app.application.execution.contracts import (
        ExecutionApproval,
        ExecutionMode,
        ExecutionStep,
        ExecutionStepRequest,
        ExecutionStepType,
    )
    from app.billing import VendorBillBuilder
    from app.billing.dto import VendorBill, VendorBillLine

    invoice = _invoice()
    source = _source(invoice)

    class _Reader:
        def get_source_invoice(
            self, *, review_id: str, company_id: int, decision_version: int
        ) -> ExecutionSourceInvoice:
            return source

    class _Builder(VendorBillBuilder):
        def __init__(self) -> None:
            self.captured = None

        def build(
            self, invoice_, partner_match, product_match, tax_match, *, company_id=None, operating_expense_match=None
        ):
            self.captured = operating_expense_match
            return VendorBill(
                supplier_id=PARTNER_ID,
                invoice_number="AKM-1",
                invoice_date=date(2026, 8, 18),
                currency="TRY",
                external_uuid=ETTN,
                reference="AKM-1",
                company_id=COMPANY_ID,
                invoice_lines=(
                    VendorBillLine(
                        product_id=None,
                        account_id=EXPENSE_ACCOUNT_ID,
                        quantity=Decimal("1"),
                        uom=None,
                        unit_price=Decimal("83.33"),
                        tax_ids=(TAX_ID,),
                        description="Common area fee 1",
                    ),
                ),
            )

    class _Writer:
        def __init__(self) -> None:
            self.commands = []

        async def write_vendor_bill(self, command):
            from app.application.dto import VendorBillWriteResult

            self.commands.append(command)
            return VendorBillWriteResult(
                status="dry_run", idempotency_key=command.idempotency_key, safe_message="dry run", success=True
            )

    builder = _Builder()
    strategy = VendorBillExecutionStrategy(
        source_invoice_reader=_Reader(),
        vendor_bill_builder=builder,
        vendor_bill_writer=_Writer(),
    )
    request = ExecutionStepRequest(
        execution_id="execution-1",
        review_id="review-1",
        company_id=COMPANY_ID,
        decision_version=2,
        mode=ExecutionMode.DRY_RUN,
        step=ExecutionStep(
            step_key="review-1:2:vendor_bill:workflow",
            step_type=ExecutionStepType.VENDOR_BILL,
            allocation_keys=(),
            sequence=1,
            execute_supported=True,
        ),
        approval=ExecutionApproval(approved_by="finance.lead"),
    )
    strategy.execute(request)

    assert builder.captured is source.operating_expense_match
    assert builder.captured.expense_account_id == EXPENSE_ACCOUNT_ID


def test_expense_source_builds_account_only_line_and_payload() -> None:
    from app.billing import VendorBillBuilder, to_odoo_account_move_payload

    invoice = _invoice()
    source = _source(invoice)

    bill = VendorBillBuilder().build(
        source.invoice,
        source.partner_match,
        source.product_match,
        source.tax_match,
        company_id=source.company_id,
        operating_expense_match=source.operating_expense_match,
    )
    assert bill.invoice_lines[0].product_id is None
    assert bill.invoice_lines[0].account_id == EXPENSE_ACCOUNT_ID

    line_payload = to_odoo_account_move_payload(bill)["invoice_line_ids"][0][2]
    assert line_payload["account_id"] == EXPENSE_ACCOUNT_ID
    assert "product_id" not in line_payload
    assert "product_uom_id" not in line_payload
    assert line_payload["tax_ids"] == ((6, 0, (TAX_ID,)),)


def test_immutable_pin_survives_a_different_current_mapping() -> None:
    """The account executed is the one pinned in Stage-2 evidence, not any 'current' value."""
    from app.billing import VendorBillBuilder

    invoice = _invoice()
    pinned_source = _source(invoice)  # expense_account_id = 9001

    # A later, different "current" mapping would say 9999 - but execution never consults it.
    drifted_match = _expense_match(expense_account_id=9999)
    assert drifted_match.expense_account_id == 9999

    bill = VendorBillBuilder().build(
        pinned_source.invoice,
        pinned_source.partner_match,
        pinned_source.product_match,
        pinned_source.tax_match,
        company_id=pinned_source.company_id,
        operating_expense_match=pinned_source.operating_expense_match,
    )
    assert bill.invoice_lines[0].account_id == EXPENSE_ACCOUNT_ID  # 9001, the pinned value


# --------------------------------------------------------------------------- migration (17-20)


def test_operating_expense_evidence_migration_upgrade_and_downgrade(tmp_path: Path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'opex_evidence_migration.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")

    command.upgrade(config, "202607170020")
    inspector = inspect(create_engine(database_url))
    stage1 = {c["name"]: c for c in inspector.get_columns("workbench_review_execution_evidence")}
    stage2 = {c["name"]: c for c in inspector.get_columns("execution_source_invoice_evidence")}
    assert "operating_expense_match" in stage1
    assert "operating_expense_match" in stage2
    assert stage1["operating_expense_match"]["nullable"] is True
    assert stage2["operating_expense_match"]["nullable"] is True

    command.downgrade(config, "202607170019")
    inspector = inspect(create_engine(database_url))
    assert "operating_expense_match" not in {
        c["name"] for c in inspector.get_columns("workbench_review_execution_evidence")
    }
    assert "operating_expense_match" not in {
        c["name"] for c in inspector.get_columns("execution_source_invoice_evidence")
    }

    command.upgrade(config, "head")
    inspector = inspect(create_engine(database_url))
    assert "operating_expense_match" in {c["name"] for c in inspector.get_columns("execution_source_invoice_evidence")}
    get_settings.cache_clear()


def test_billing_and_evidence_packages_have_no_mapping_repository_in_execution_read_path() -> None:
    reader_source = Path("app/persistence/execution_source_invoice_reader.py").read_text(encoding="utf-8")
    strategy_source = Path("app/application/execution/vendor_bill_strategy.py").read_text(encoding="utf-8")
    for source in (reader_source, strategy_source):
        assert "OperatingExpenseMappingRepository" not in source
        assert "SqlAlchemyOperatingExpenseMappingRepository" not in source
        assert "OperatingExpenseMatchingEngine" not in source
        assert "find_for_supplier" not in source
