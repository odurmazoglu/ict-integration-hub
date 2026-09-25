"""Actual deterministic classification -> immutable Stage 1 -> explicit decision -> Stage 2.

All persistence is in-memory; selected account/product readers and the writer are fakes.
Exercises the supplier-resolution reclassification checkpoint without an ERP write.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

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
from app.application.dto import VendorBillWriteResult
from app.application.execution import VendorBillExecutionStrategy
from app.application.execution.contracts import (
    ExecutionMode,
    ExecutionStep,
    ExecutionStepRequest,
    ExecutionStepStatus,
    ExecutionStepType,
)
from app.application.execution.exceptions import (
    ExecutionSourceInvoiceIntegrityError,
    ExecutionSourceInvoiceNotFoundError,
)
from app.application.rules.deterministic import DeterministicRuleEngine
from app.application.use_cases import ImportInvoiceUseCase
from app.application.use_cases.reclassify_review import ReclassifyWorkbenchReviewUseCase
from app.application.workbench import (
    LineResolution,
    ReviewDecisionCommand,
    ReviewDecisionType,
    ReviewItemCreationService,
)
from app.application.workbench.decision_use_cases import SubmitReviewDecisionUseCase
from app.application.workbench.exceptions import (
    ReviewDecisionError,
    ReviewDecisionIdempotencyConflictError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
    WorkbenchContractError,
)
from app.application.workbench.reclassification import ReclassifyReviewCommand, ReviewReclassificationTrigger
from app.application.workbench.selected_expense_account_resolution import ResolutionAccountRecord
from app.application.workbench.selected_product_resolution import ResolutionProductRecord
from app.application.workflow import (
    ManualReviewReasonCode,
    WorkflowType,
)
from app.billing import VendorBillBuilder
from app.db.base import Base
from app.domain.invoice import Discount, Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
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
from app.models.workbench_review_reclassification import WorkbenchReviewReclassification
from app.models.workbench_review_source_invoice_evidence import WorkbenchReviewSourceInvoiceEvidence
from app.persistence import (
    SqlAlchemyExecutionSourceInvoiceReader,
    SqlAlchemyReviewRepository,
    SqlAlchemyReviewSourceInvoiceEvidenceReader,
    SqlAlchemyUnitOfWork,
)
from app.persistence.review_execution_evidence_reader import SqlAlchemyReviewExecutionEvidenceReader
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType
from tests.unit.resale_execution_support import NON_RESALE_ACCOUNTING_CHECK

COMPANY_ID = 7
IDEMPOTENCY_KEY = "uyumsoft:7:DMARKET-ETTN"
ETTN = "DMARKET-ETTN"
SELLER_ITEM_CODE = "HBV000006MHLQ"
NEW_PARTNER_ID = 44801  # fixture id standing in for the future real Odoo partner
TAX_ID = 3401
TEST_EXPENSE_ACCOUNT_ID = 92470  # fixture id -- never the real production account
TEST_PRODUCT_ID = 55501  # fixture id, for the selected-product regression (Step F)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewExecutionEvidence.__table__,
            WorkbenchReviewClassificationEvidence.__table__,
            WorkbenchReviewSourceInvoiceEvidence.__table__,
            WorkbenchReviewReclassification.__table__,
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


class _MatchingFacts:
    def __init__(self, result) -> None:
        self.result = result

    def match_invoice(self, invoice: InternalInvoice, *, company_id: int, partner_match: object = None):
        return self.result

    def map_invoice(self, invoice: InternalInvoice, *, company_id: int):
        return self.result


class _FakeSelectedAccountReader:
    def __init__(self, *, records: tuple[ResolutionAccountRecord, ...] = ()) -> None:
        self._records = records

    def find_accounts_by_ids(self, account_ids: tuple[int, ...]) -> tuple[ResolutionAccountRecord, ...]:
        return tuple(r for r in self._records if r.id in account_ids)


class _FakeSelectedProductReader:
    def __init__(self, *, records: tuple[ResolutionProductRecord, ...] = ()) -> None:
        self._records = records

    def find_products_by_ids(self, product_ids: tuple[int, ...]) -> tuple[ResolutionProductRecord, ...]:
        return tuple(r for r in self._records if r.id in product_ids)


def _invoice() -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="HD1",
            invoice_uuid=ETTN,
            ettn=ETTN,
            issue_date=date(2026, 9, 10),
            currency_code="TRY",
        ),
        supplier=Party(name="D-MARKET ELEKTRONIK HIZMETLER VE TICARET ANONIM SIRKETI", tax_number="2650179910"),
        customer=Party(name="ICT", tax_number="4651205941"),
        totals=MonetaryTotals(
            line_extension_amount=Decimal("563.51"),
            tax_exclusive_amount=Decimal("563.51"),
            tax_inclusive_amount=Decimal("676.21"),
            allowance_total=Decimal("241.50"),
            payable_amount=Decimal("676.21"),
        ),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Kraf Kesim Tablasi A2 45X60 3002G",
                seller_item_code=SELLER_ITEM_CODE,
                quantity=Decimal("1.000"),
                unit_code="C62",
                unit_price=Decimal("805.010000"),
                line_extension_amount=Decimal("563.51"),
                discounts=(Discount(amount=Decimal("241.50")),),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
            ),
        ),
    )


def _partner(status: PartnerMatchStatus, *, partner_id: int | None = None) -> PartnerMatchResult:
    matched = status is PartnerMatchStatus.MATCHED
    return PartnerMatchResult(
        status=status,
        partner_id=partner_id if matched else None,
        matched_by="tax_number" if matched else None,
        reason="matched" if matched else "unmatched",
        candidate_count=1 if matched else 0,
        confidence=Decimal("1.00") if matched else None,
    )


def _product_not_found(invoice: InternalInvoice) -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(
        line_results=tuple(
            InvoiceProductLineResult(
                line_number=line.line_number,
                result=ProductMatchResult(
                    status=ProductMatchStatus.NOT_FOUND,
                    line_number=line.line_number,
                    product_id=None,
                    default_code=None,
                    barcode=None,
                    seller_item_code=line.seller_item_code,
                    matched_by=None,
                    reason="not found",
                    candidate_count=0,
                    confidence=None,
                ),
            )
            for line in invoice.lines
        )
    )


def _taxes(invoice: InternalInvoice) -> InvoiceTaxMappingResult:
    return InvoiceTaxMappingResult(
        line_results=tuple(
            InvoiceTaxLineResult(
                line_number=line.line_number,
                tax_index=idx,
                result=TaxMatchResult(
                    status=TaxMatchStatus.MATCHED,
                    tax_id=TAX_ID,
                    company_id=COMPANY_ID,
                    tax_type=TaxType.VAT,
                    tax_rate=Decimal("20"),
                    matched_by="company_type_rate",
                    confidence=Decimal("1.00"),
                    reason="matched",
                    candidate_count=1,
                ),
            )
            for line in invoice.lines
            for idx, _t in enumerate(line.taxes)
        )
    )


@dataclass(frozen=True)
class _Facts:
    partner_match: PartnerMatchResult
    product_match: InvoiceProductMatchResult
    tax_match: InvoiceTaxMappingResult


def _initial_facts(invoice: InternalInvoice) -> _Facts:
    return _Facts(_partner(PartnerMatchStatus.NOT_FOUND), _product_not_found(invoice), _taxes(invoice))


def _resolved_facts(invoice: InternalInvoice) -> _Facts:
    return _Facts(
        _partner(PartnerMatchStatus.MATCHED, partner_id=NEW_PARTNER_ID), _product_not_found(invoice), _taxes(invoice)
    )


def _decision_engine(rule_result: _Facts) -> DecisionEngine:
    return DecisionEngine(
        rule_engine=DeterministicRuleEngine(
            partner_matcher=_MatchingFacts(rule_result.partner_match),
            product_matcher=_MatchingFacts(rule_result.product_match),
            tax_mapper=_MatchingFacts(rule_result.tax_match),
        ),
        strategy_resolver=WorkflowStrategyResolver([VendorBillReviewRecommendationStrategy(), ManualReviewStrategy()]),
    )


async def _import(session: Session, rule_result: _Facts, *, invoice: InternalInvoice | None = None) -> str:
    use_case = ImportInvoiceUseCase(
        import_history=_FakeImportHistory(),
        decision_engine=_decision_engine(rule_result),
        review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(session)),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )
    result = await use_case.execute(
        ImportInvoiceCommand(invoice=invoice or _invoice(), idempotency_key=IDEMPOTENCY_KEY, company_id=COMPANY_ID)
    )
    assert result.review_id is not None
    return result.review_id


async def _reclassify(session: Session, *, review_id: str, expected_version: int, rule_result: _Facts):
    use_case = ReclassifyWorkbenchReviewUseCase(
        decision_engine=_decision_engine(rule_result),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        reclassification_writer=SqlAlchemyReviewRepository(session),
    )
    outcome = await use_case.execute(
        ReclassifyReviewCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=expected_version,
            trigger=ReviewReclassificationTrigger.SUPPLIER_RESOLUTION,
        )
    )
    session.commit()
    return outcome


def _submit_decision(
    session: Session,
    *,
    review_id: str,
    expected_version: int,
    line_resolutions: tuple[LineResolution, ...],
    account_records: tuple[ResolutionAccountRecord, ...] = (),
    product_records: tuple[ResolutionProductRecord, ...] = (),
    idempotency_key: str | None = None,
):
    use_case = SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        selected_product_reader=_FakeSelectedProductReader(records=product_records),
        selected_account_reader=_FakeSelectedAccountReader(records=account_records),
    )
    result = use_case.execute(
        ReviewDecisionCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=expected_version,
            decision=ReviewDecisionType.SELECT_WORKFLOW,
            decided_by="operator",
            idempotency_key=idempotency_key or f"decision:{review_id}:{expected_version}",
            selected_workflow=WorkflowType.VENDOR_BILL,
            line_resolutions=line_resolutions,
        )
    )
    session.commit()
    return result


async def test_e_real_pilot_lifecycle_account_only_decision_is_accepted(session: Session) -> None:
    invoice = _invoice()
    review_id = await _import(session, _initial_facts(invoice))

    initial = session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_id))
    assert initial.version == 1
    assert initial.workflow == WorkflowType.MANUAL_REVIEW.value
    # v1 correctly has NO Stage-1 evidence -- the mixed-invoice branch requires a MATCHED
    # partner, which does not exist yet (this is unaffected by P0-PROD-08M; it is v2,
    # after supplier resolution, that the fix changes -- checked below).
    assert (
        session.scalar(
            select(WorkbenchReviewExecutionEvidence).where(WorkbenchReviewExecutionEvidence.review_id == review_id)
        )
        is None
    )

    reclass = await _reclassify(
        session,
        review_id=review_id,
        expected_version=1,
        rule_result=_resolved_facts(invoice),
    )
    assert reclass.changed is True
    assert reclass.to_version == 2
    assert [r.code for r in reclass.new_review_reasons] == [ManualReviewReasonCode.PRODUCT_NOT_FOUND]

    v2_evidence = session.scalar(
        select(WorkbenchReviewExecutionEvidence).where(
            WorkbenchReviewExecutionEvidence.review_id == review_id,
            WorkbenchReviewExecutionEvidence.review_version == 2,
        )
    )
    assert v2_evidence is not None, "P0-PROD-08M: v2 Stage-1 evidence must now exist"

    ack = _submit_decision(
        session,
        review_id=review_id,
        expected_version=2,
        line_resolutions=(
            LineResolution(line_number="1", account_only=True, expense_account_id=TEST_EXPENSE_ACCOUNT_ID),
        ),
        account_records=(ResolutionAccountRecord(id=TEST_EXPENSE_ACCOUNT_ID, company_ids=(COMPANY_ID,)),),
    )
    assert ack.accepted is True

    stage2 = session.scalar(
        select(ExecutionSourceInvoiceEvidence).where(ExecutionSourceInvoiceEvidence.review_id == review_id)
    )
    assert stage2 is not None
    assert stage2.decision_version == 3
    assert stage2.partner_match["partner_id"] == NEW_PARTNER_ID

    decision_row = session.scalar(select(WorkbenchReviewDecision).where(WorkbenchReviewDecision.review_id == review_id))
    assert decision_row is not None
    assert len(decision_row.line_resolutions) == 1
    persisted_line = decision_row.line_resolutions[0]
    assert persisted_line["line_number"] == "1"
    assert persisted_line["account_only"] is True
    assert persisted_line["expense_account_id"] == TEST_EXPENSE_ACCOUNT_ID
    assert persisted_line.get("selected_product_id") is None

    # VendorBillExecutionStrategy consumes only pinned evidence -- no live Odoo lookup.
    source = SqlAlchemyReviewExecutionEvidenceReader(session).get_evidence(
        review_id=review_id, company_id=COMPANY_ID, expected_version=2
    )
    assert source.partner_match.partner_id == NEW_PARTNER_ID
    assert source.product_match.line_results[0].result.status is ProductMatchStatus.NOT_FOUND


async def test_f_selected_product_operator_resolution_is_also_accepted(session: Session) -> None:

    invoice = _invoice()
    review_id = await _import(session, _initial_facts(invoice))
    await _reclassify(
        session,
        review_id=review_id,
        expected_version=1,
        rule_result=_resolved_facts(invoice),
    )

    ack = _submit_decision(
        session,
        review_id=review_id,
        expected_version=2,
        line_resolutions=(LineResolution(line_number="1", selected_product_id=TEST_PRODUCT_ID),),
        product_records=(
            ResolutionProductRecord(
                id=TEST_PRODUCT_ID,
                name="Kraf Kesim Tablasi",
                default_code=None,
                barcode=None,
                active=True,
                company_id=None,
            ),
        ),
    )
    assert ack.accepted is True

    decision_row = session.scalar(select(WorkbenchReviewDecision).where(WorkbenchReviewDecision.review_id == review_id))
    persisted_line = decision_row.line_resolutions[0]
    assert persisted_line["selected_product_id"] == TEST_PRODUCT_ID
    assert persisted_line.get("account_only") in (False, None)
    assert persisted_line.get("expense_account_id") is None

    stage2 = session.scalar(
        select(ExecutionSourceInvoiceEvidence).where(ExecutionSourceInvoiceEvidence.review_id == review_id)
    )
    # The pinned product_match line is substituted to a human-selected MATCHED result --
    # never re-queried from Odoo at execution time.
    assert stage2.product_match["line_results"][0]["result"]["product_id"] == TEST_PRODUCT_ID
    assert stage2.product_match["line_results"][0]["result"]["matched_by"] == "human_selected"


async def _resolved_review(session: Session) -> str:
    invoice = _invoice()
    review_id = await _import(session, _initial_facts(invoice))
    await _reclassify(
        session,
        review_id=review_id,
        expected_version=1,
        rule_result=_resolved_facts(invoice),
    )
    return review_id


async def test_g1_unresolved_line_with_no_operator_override_rejects_decision(session: Session) -> None:
    review_id = await _resolved_review(session)
    with pytest.raises(ReviewDecisionError, match="complete resolved execution inputs"):
        _submit_decision(session, review_id=review_id, expected_version=2, line_resolutions=())
    _assert_no_decision(session)
    evidence = session.scalar(select(WorkbenchReviewExecutionEvidence))
    assert evidence.product_match["line_results"][0]["result"]["product_id"] is None
    assert evidence.operating_expense_match is None


async def test_new_account_only_without_account_is_rejected_at_application_boundary(session: Session) -> None:
    review_id = await _resolved_review(session)
    with pytest.raises(ReviewDecisionError, match="explicit expense_account_id"):
        _submit_decision(
            session,
            review_id=review_id,
            expected_version=2,
            line_resolutions=(LineResolution(line_number="1", account_only=True),),
        )
    _assert_no_decision(session)


def _assert_no_decision(session: Session) -> None:
    assert session.scalar(select(WorkbenchReviewDecision)) is None
    assert session.scalar(select(ExecutionSourceInvoiceEvidence)) is None
    item = session.scalar(select(WorkbenchReviewItem))
    assert item.version == 2
    assert item.status == "pending_review"


def test_g2_account_only_without_expense_account_id_is_rejected_by_the_rest_contract() -> None:

    from app.schemas.workbench import LineResolutionRequest

    with pytest.raises(ValueError):
        LineResolutionRequest(line_number="1", account_only=True)


def test_g3_selected_product_and_account_only_together_is_rejected() -> None:
    with pytest.raises(WorkbenchContractError):
        LineResolution(line_number="1", selected_product_id=TEST_PRODUCT_ID, account_only=True)


async def test_g4_expense_account_missing_in_odoo_reader_fails_closed(session: Session) -> None:
    review_id = await _resolved_review(session)
    with pytest.raises(ReviewDecisionError):
        _submit_decision(
            session,
            review_id=review_id,
            expected_version=2,
            line_resolutions=(
                LineResolution(line_number="1", account_only=True, expense_account_id=TEST_EXPENSE_ACCOUNT_ID),
            ),
            account_records=(),  # reader finds nothing
        )
    _assert_no_decision(session)


async def test_g5_expense_account_scoped_to_another_company_fails_closed(session: Session) -> None:
    review_id = await _resolved_review(session)
    with pytest.raises(ReviewDecisionError):
        _submit_decision(
            session,
            review_id=review_id,
            expected_version=2,
            line_resolutions=(
                LineResolution(line_number="1", account_only=True, expense_account_id=TEST_EXPENSE_ACCOUNT_ID),
            ),
            account_records=(ResolutionAccountRecord(id=TEST_EXPENSE_ACCOUNT_ID, company_ids=(COMPANY_ID + 1,)),),
        )
    _assert_no_decision(session)


async def test_g6_selected_product_missing_in_odoo_reader_fails_closed(session: Session) -> None:
    review_id = await _resolved_review(session)
    with pytest.raises(ReviewDecisionError):
        _submit_decision(
            session,
            review_id=review_id,
            expected_version=2,
            line_resolutions=(LineResolution(line_number="1", selected_product_id=TEST_PRODUCT_ID),),
            product_records=(),  # reader finds nothing
        )
    _assert_no_decision(session)


async def test_g7_missing_supplier_match_never_pins_evidence_and_decision_fails_closed(session: Session) -> None:

    invoice = _invoice()
    review_id = await _import(session, _initial_facts(invoice))
    still_unresolved = _initial_facts(invoice)  # identical to v1 -- a genuine no-op reclassification
    reclass = await _reclassify(session, review_id=review_id, expected_version=1, rule_result=still_unresolved)
    assert reclass.changed is False
    assert reclass.to_version == 1

    assert (
        session.scalar(
            select(WorkbenchReviewExecutionEvidence).where(
                WorkbenchReviewExecutionEvidence.review_id == review_id,
                WorkbenchReviewExecutionEvidence.review_version == 1,
            )
        )
        is None
    )
    with pytest.raises(ExecutionSourceInvoiceNotFoundError):
        _submit_decision(
            session,
            review_id=review_id,
            expected_version=1,
            line_resolutions=(
                LineResolution(line_number="1", account_only=True, expense_account_id=TEST_EXPENSE_ACCOUNT_ID),
            ),
            account_records=(ResolutionAccountRecord(id=TEST_EXPENSE_ACCOUNT_ID, company_ids=(COMPANY_ID,)),),
        )


async def test_g8_ambiguous_tax_match_never_pins_evidence_and_decision_fails_closed(session: Session) -> None:

    invoice = _invoice()
    review_id = await _import(session, _initial_facts(invoice))
    unmatched_tax = InvoiceTaxMappingResult(
        line_results=tuple(
            InvoiceTaxLineResult(
                line_number=line.line_number,
                tax_index=idx,
                result=TaxMatchResult(
                    status=TaxMatchStatus.NOT_FOUND,
                    tax_id=None,
                    company_id=None,
                    tax_type=TaxType.VAT,
                    tax_rate=Decimal("20"),
                    matched_by=None,
                    confidence=None,
                    reason="ambiguous",
                    candidate_count=2,
                ),
            )
            for line in invoice.lines
            for idx, _t in enumerate(line.taxes)
        )
    )
    rule_result = _Facts(
        partner_match=_partner(PartnerMatchStatus.MATCHED, partner_id=NEW_PARTNER_ID),
        product_match=_product_not_found(invoice),
        tax_match=unmatched_tax,
    )
    await _reclassify(session, review_id=review_id, expected_version=1, rule_result=rule_result)

    assert (
        session.scalar(
            select(WorkbenchReviewExecutionEvidence).where(
                WorkbenchReviewExecutionEvidence.review_id == review_id,
                WorkbenchReviewExecutionEvidence.review_version == 2,
            )
        )
        is None
    )
    with pytest.raises(ExecutionSourceInvoiceNotFoundError):
        _submit_decision(
            session,
            review_id=review_id,
            expected_version=2,
            line_resolutions=(
                LineResolution(line_number="1", account_only=True, expense_account_id=TEST_EXPENSE_ACCOUNT_ID),
            ),
            account_records=(ResolutionAccountRecord(id=TEST_EXPENSE_ACCOUNT_ID, company_ids=(COMPANY_ID,)),),
        )


class _CapturingWriter:
    def __init__(self) -> None:
        self.commands = []

    async def write_vendor_bill(self, command):
        self.commands.append(command)
        return VendorBillWriteResult(
            success=True,
            status="dry_run",
            idempotency_key=command.idempotency_key,
            safe_message="Fixture only.",
        )


def _execute_pinned(session: Session, review_id: str):
    writer = _CapturingWriter()
    result = VendorBillExecutionStrategy(
        source_invoice_reader=SqlAlchemyExecutionSourceInvoiceReader(session),
        vendor_bill_builder=VendorBillBuilder(),
        vendor_bill_writer=writer,
        resale_accounting_check=NON_RESALE_ACCOUNTING_CHECK,
    ).execute(
        ExecutionStepRequest(
            execution_id="fixture-execution",
            review_id=review_id,
            company_id=COMPANY_ID,
            decision_version=3,
            mode=ExecutionMode.DRY_RUN,
            step=ExecutionStep(
                step_key="vendor_bill",
                step_type=ExecutionStepType.VENDOR_BILL,
                allocation_keys=(),
                sequence=1,
                execute_supported=True,
            ),
        )
    )
    assert result.status is ExecutionStepStatus.DRY_RUN_OK
    assert len(writer.commands) == 1
    assert writer.commands[0].dry_run is True
    return writer.commands[0].vendor_bill


def _product_record() -> ResolutionProductRecord:
    return ResolutionProductRecord(
        id=TEST_PRODUCT_ID,
        name="Selected product",
        default_code=None,
        barcode=None,
        active=True,
        company_id=None,
    )


@pytest.mark.parametrize("account_only", [True, False])
async def test_discounted_resolution_executes_only_pinned_evidence(session: Session, monkeypatch, account_only: bool):
    review_id = await _resolved_review(session)
    immutable_source = session.scalar(select(WorkbenchReviewSourceInvoiceEvidence)).invoice.copy()
    stage1_product = session.scalar(select(WorkbenchReviewExecutionEvidence)).product_match.copy()
    resolutions = (
        (LineResolution(line_number="1", account_only=True, expense_account_id=TEST_EXPENSE_ACCOUNT_ID),)
        if account_only
        else (LineResolution(line_number="1", selected_product_id=TEST_PRODUCT_ID),)
    )
    _submit_decision(
        session,
        review_id=review_id,
        expected_version=2,
        line_resolutions=resolutions,
        account_records=(ResolutionAccountRecord(id=TEST_EXPENSE_ACCOUNT_ID, company_ids=(COMPANY_ID,)),),
        product_records=(_product_record(),),
    )
    assert session.scalar(select(WorkbenchReviewSourceInvoiceEvidence)).invoice == immutable_source
    assert session.scalar(select(WorkbenchReviewExecutionEvidence)).product_match == stage1_product

    def forbidden_lookup(*args, **kwargs):
        pytest.fail("Execution must never read live selected account/product data.")

    monkeypatch.setattr(_FakeSelectedAccountReader, "find_accounts_by_ids", forbidden_lookup)
    monkeypatch.setattr(_FakeSelectedProductReader, "find_products_by_ids", forbidden_lookup)
    # Replace only the async writer runner; the real strategy and persisted readers run.
    monkeypatch.setattr(
        "app.application.execution.vendor_bill_strategy._run_writer",
        lambda *, writer, command: _capture_fixture_command(writer, command),
    )
    bill = _execute_pinned(session, review_id)
    line = bill.invoice_lines[0]
    assert line.account_id == (TEST_EXPENSE_ACCOUNT_ID if account_only else None)
    assert line.product_id == (None if account_only else TEST_PRODUCT_ID)
    untaxed = line.quantity * line.unit_price
    vat = (untaxed * Decimal("0.20")).quantize(Decimal("0.01"))
    assert untaxed == Decimal("563.51")
    assert vat == Decimal("112.70")
    assert untaxed + vat == Decimal("676.21")
    assert line.tax_ids == (TAX_ID,)


def _capture_fixture_command(writer, command):
    writer.commands.append(command)
    return VendorBillWriteResult(success=True, status="dry_run", idempotency_key=command.idempotency_key)


async def test_mixed_explicit_resolutions_are_pinned_and_build(session: Session, monkeypatch):
    original = _invoice()
    invoice = replace(
        original,
        lines=(original.lines[0], replace(original.lines[0], line_number="2")),
        totals=MonetaryTotals(
            line_extension_amount=Decimal("1127.02"),
            tax_exclusive_amount=Decimal("1127.02"),
            tax_inclusive_amount=Decimal("1352.42"),
            payable_amount=Decimal("1352.42"),
        ),
    )
    review_id = await _import(session, _initial_facts(invoice), invoice=invoice)
    await _reclassify(session, review_id=review_id, expected_version=1, rule_result=_resolved_facts(invoice))
    resolutions = (
        LineResolution(line_number="1", selected_product_id=TEST_PRODUCT_ID),
        LineResolution(line_number="2", account_only=True, expense_account_id=TEST_EXPENSE_ACCOUNT_ID),
    )
    _submit_decision(
        session,
        review_id=review_id,
        expected_version=2,
        line_resolutions=resolutions,
        account_records=(ResolutionAccountRecord(id=TEST_EXPENSE_ACCOUNT_ID, company_ids=(COMPANY_ID,)),),
        product_records=(_product_record(),),
    )
    source = SqlAlchemyExecutionSourceInvoiceReader(session).get_source_invoice(
        review_id=review_id,
        company_id=COMPANY_ID,
        decision_version=3,
    )
    assert source.line_resolutions == resolutions
    assert source.product_match.line_results[0].result.product_id == TEST_PRODUCT_ID
    assert source.product_match.line_results[1].result.product_id is None
    monkeypatch.setattr(
        "app.application.execution.vendor_bill_strategy._run_writer",
        lambda *, writer, command: _capture_fixture_command(writer, command),
    )
    bill = _execute_pinned(session, review_id)
    assert bill.invoice_lines[0].product_id == TEST_PRODUCT_ID
    assert bill.invoice_lines[0].account_id is None
    assert bill.invoice_lines[1].account_id == TEST_EXPENSE_ACCOUNT_ID
    assert bill.invoice_lines[1].product_id is None


async def test_decision_replay_and_conflicting_key_and_stale_version(session: Session):
    review_id = await _resolved_review(session)
    args = dict(
        session=session,
        review_id=review_id,
        expected_version=2,
        line_resolutions=(
            LineResolution(line_number="1", account_only=True, expense_account_id=TEST_EXPENSE_ACCOUNT_ID),
        ),
        account_records=(ResolutionAccountRecord(id=TEST_EXPENSE_ACCOUNT_ID, company_ids=(COMPANY_ID,)),),
    )
    first = _submit_decision(**args)
    assert _submit_decision(**args) == first
    assert len(session.scalars(select(WorkbenchReviewDecision)).all()) == 1
    assert len(session.scalars(select(ExecutionSourceInvoiceEvidence)).all()) == 1
    with pytest.raises(ReviewStateConflictError):
        _submit_decision(**args, idempotency_key="new-key-for-stale-version")
    with pytest.raises(ReviewDecisionIdempotencyConflictError):
        _submit_decision(
            session,
            review_id=review_id,
            expected_version=2,
            line_resolutions=(LineResolution(line_number="1", selected_product_id=TEST_PRODUCT_ID),),
            product_records=(_product_record(),),
        )


@pytest.mark.parametrize("corruption", ["missing", "product_scope", "invoice", "schema"])
async def test_missing_or_corrupt_snapshot_never_persists_decision(session: Session, corruption: str):
    review_id = await _resolved_review(session)
    row = session.scalar(select(WorkbenchReviewExecutionEvidence))
    if corruption == "missing":
        session.delete(row)
    elif corruption == "product_scope":
        row.product_match = {"line_results": [], "warnings": [], "errors": []}
    elif corruption == "invoice":
        row.invoice = {}
    else:
        row.schema_version = 999
    session.commit()
    with pytest.raises((ExecutionSourceInvoiceNotFoundError, ExecutionSourceInvoiceIntegrityError)):
        _submit_decision(
            session,
            review_id=review_id,
            expected_version=2,
            line_resolutions=(
                LineResolution(line_number="1", account_only=True, expense_account_id=TEST_EXPENSE_ACCOUNT_ID),
            ),
            account_records=(ResolutionAccountRecord(id=TEST_EXPENSE_ACCOUNT_ID, company_ids=(COMPANY_ID,)),),
        )
    _assert_no_decision(session)


async def test_evidence_persistence_failure_rolls_back_acceptance(session: Session, monkeypatch):
    review_id = await _resolved_review(session)

    def fail_capture(*args, **kwargs):
        raise ReviewDecisionError("Injected evidence capture failure.")

    monkeypatch.setattr(SqlAlchemyReviewRepository, "_add_execution_source_evidence", fail_capture)
    with pytest.raises(ReviewDecisionError, match="Injected"):
        _submit_decision(
            session,
            review_id=review_id,
            expected_version=2,
            line_resolutions=(
                LineResolution(line_number="1", account_only=True, expense_account_id=TEST_EXPENSE_ACCOUNT_ID),
            ),
            account_records=(ResolutionAccountRecord(id=TEST_EXPENSE_ACCOUNT_ID, company_ids=(COMPANY_ID,)),),
        )
    _assert_no_decision(session)


async def test_preexisting_legacy_decision_replay_keeps_original_contract(session: Session):
    review_id = await _resolved_review(session)
    command = ReviewDecisionCommand(
        review_id=review_id,
        company_id=COMPANY_ID,
        expected_version=2,
        decision=ReviewDecisionType.SELECT_WORKFLOW,
        decided_by="operator",
        idempotency_key=f"decision:{review_id}:2",
        selected_workflow=WorkflowType.VENDOR_BILL,
        line_resolutions=(LineResolution(line_number="1", account_only=True),),
    )
    # Seed through the pre-existing repository API, representing a decision accepted
    # before the explicit-account contract; no production data or manual DB patch.
    evidence = SqlAlchemyReviewExecutionEvidenceReader(session).get_evidence(
        review_id=review_id,
        company_id=COMPANY_ID,
        expected_version=2,
    )
    expected = SqlAlchemyReviewRepository(session).submit_review_decision_with_execution_evidence(command, evidence)
    session.commit()
    replayed = _submit_decision(
        session, review_id=review_id, expected_version=2, line_resolutions=command.line_resolutions
    )
    assert replayed == expected
    assert len(session.scalars(select(WorkbenchReviewDecision)).all()) == 1


async def test_pending_review_version_conflict_is_preserved(session: Session) -> None:
    review_id = await _resolved_review(session)
    facts = _resolved_facts(_invoice())
    line = facts.product_match.line_results[0]
    matched = replace(
        facts,
        product_match=replace(
            facts.product_match,
            line_results=(
                replace(
                    line, result=replace(line.result, status=ProductMatchStatus.MATCHED, product_id=TEST_PRODUCT_ID)
                ),
            ),
        ),
    )
    reclassified = await _reclassify(session, review_id=review_id, expected_version=2, rule_result=matched)
    assert reclassified.to_version == 3
    with pytest.raises(ReviewVersionConflictError):
        _submit_decision(
            session,
            review_id=review_id,
            expected_version=2,
            line_resolutions=(LineResolution(line_number="1", selected_product_id=TEST_PRODUCT_ID),),
            product_records=(_product_record(),),
        )
    assert session.scalar(select(WorkbenchReviewDecision)) is None
    assert session.scalar(select(ExecutionSourceInvoiceEvidence)) is None
    assert session.scalar(select(WorkbenchReviewItem)).version == 3


async def test_unknown_resolution_line_cannot_be_silently_dropped(session: Session) -> None:
    review_id = await _resolved_review(session)
    with pytest.raises(ReviewDecisionError, match="unknown invoice line"):
        _submit_decision(
            session,
            review_id=review_id,
            expected_version=2,
            line_resolutions=(
                LineResolution(line_number="missing", account_only=True, expense_account_id=TEST_EXPENSE_ACCOUNT_ID),
            ),
            account_records=(ResolutionAccountRecord(id=TEST_EXPENSE_ACCOUNT_ID, company_ids=(COMPANY_ID,)),),
        )
    _assert_no_decision(session)


async def _historical_v2_without_snapshot(session: Session, monkeypatch) -> str:
    """Emulate the pre-08M builder at the original transition, never repair history."""
    invoice = _invoice()
    review_id = await _import(session, _initial_facts(invoice))
    with monkeypatch.context() as old_code:
        old_code.setattr(
            "app.application.use_cases.reclassify_review.build_review_execution_evidence", lambda **_: None
        )
        result = await _reclassify(
            session, review_id=review_id, expected_version=1, rule_result=_resolved_facts(invoice)
        )
    assert result.to_version == 2
    assert result.executable is False
    return review_id


async def _fresh_matching_snapshot(session: Session, review_id: str, *, expected_version: int = 2, facts=None):
    """Existing supported application operation; no remediation orchestration."""
    result = await ReclassifyWorkbenchReviewUseCase(
        decision_engine=_decision_engine(facts or _resolved_facts(_invoice())),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        reclassification_writer=SqlAlchemyReviewRepository(session),
    ).execute(
        ReclassifyReviewCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=expected_version,
            trigger=ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
            note="Explicit current-time matching refresh after snapshot retention fix; not historical recovery.",
        )
    )
    session.commit()
    return result


async def test_08o_existing_reclassification_versions_new_snapshot_without_business_change(session, monkeypatch):
    review_id = await _historical_v2_without_snapshot(session, monkeypatch)
    source_before = session.scalar(select(WorkbenchReviewSourceInvoiceEvidence)).invoice.copy()
    v2_event = session.scalar(select(WorkbenchReviewReclassification))
    v2_event_before = (v2_event.to_version, v2_event.new_workflow, v2_event.new_review_reasons, v2_event.executable)
    review = session.scalar(select(WorkbenchReviewItem))
    reasons_before = review.review_reasons.copy()

    result = await _fresh_matching_snapshot(session, review_id)
    session.expire_all()
    assert result.changed and result.to_version == 3
    assert result.previous_workflow == result.new_workflow == WorkflowType.MANUAL_REVIEW
    assert result.previous_review_reasons == result.new_review_reasons
    assert [r.code for r in result.new_review_reasons] == [ManualReviewReasonCode.PRODUCT_NOT_FOUND]
    assert session.scalar(select(WorkbenchReviewItem)).version == 3
    assert session.scalar(select(WorkbenchReviewItem)).status == "pending_review"
    assert session.scalar(select(WorkbenchReviewItem)).review_reasons == reasons_before
    assert session.scalar(select(WorkbenchReviewSourceInvoiceEvidence)).invoice == source_before
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 1
    row = session.scalar(select(WorkbenchReviewExecutionEvidence))
    assert row.review_version == 3  # v2 remains missing, never backfilled
    assert row.partner_match["partner_id"] == NEW_PARTNER_ID
    assert row.product_match["line_results"][0]["result"]["status"] == ProductMatchStatus.NOT_FOUND.value
    assert row.tax_match["line_results"][0]["result"]["tax_id"] == TAX_ID
    for forbidden in ("account_only", "expense_account_id", "selected_product_id"):
        assert forbidden not in str(row.invoice) + str(row.product_match)
    assert row.operating_expense_match is None
    assert row.account_only_expense_match["status"] == "NOT_FOUND"
    assert row.account_only_expense_match["expense_account_id"] is None
    old_event = session.get(WorkbenchReviewReclassification, v2_event.id)
    assert (
        old_event.to_version,
        old_event.new_workflow,
        old_event.new_review_reasons,
        old_event.executable,
    ) == v2_event_before
    new_event = session.scalar(
        select(WorkbenchReviewReclassification).where(WorkbenchReviewReclassification.from_version == 2)
    )
    assert new_event.trigger == "master_data_changed"
    assert "current-time" in new_event.note and new_event.created_at is not None
    assert session.query(WorkbenchReviewDecision).count() == 0
    assert session.query(ExecutionSourceInvoiceEvidence).count() == 0


async def test_08o_v3_explicit_account_only_pins_stage2_and_preserves_discount(session, monkeypatch):
    review_id = await _historical_v2_without_snapshot(session, monkeypatch)
    await _fresh_matching_snapshot(session, review_id)
    ack = _submit_decision(
        session,
        review_id=review_id,
        expected_version=3,
        line_resolutions=(LineResolution(line_number="1", account_only=True, expense_account_id=247),),
        account_records=(ResolutionAccountRecord(id=247, company_ids=(COMPANY_ID,)),),
    )
    assert ack.accepted
    source = SqlAlchemyExecutionSourceInvoiceReader(session).get_source_invoice(
        review_id=review_id, company_id=COMPANY_ID, decision_version=4
    )
    assert source.line_resolutions[0].expense_account_id == 247
    assert source.product_match.line_results[0].result.status is ProductMatchStatus.NOT_FOUND
    bill = VendorBillBuilder().build(
        source.invoice,
        source.partner_match,
        source.product_match,
        source.tax_match,
        company_id=COMPANY_ID,
        account_only_line_numbers=frozenset({"1"}),
        explicit_account_only_accounts={"1": source.line_resolutions[0].expense_account_id},
    )
    line = bill.invoice_lines[0]
    assert line.account_id == 247 and line.product_id is None
    untaxed = line.quantity * line.unit_price
    vat = (untaxed * Decimal("0.20")).quantize(Decimal("0.01"))
    assert (untaxed, vat, untaxed + vat) == (Decimal("563.51"), Decimal("112.70"), Decimal("676.21"))
    assert line.tax_ids == (TAX_ID,)
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 1
    assert session.scalar(select(WorkbenchReviewExecutionEvidence)).review_version == 3


async def test_08o_replay_and_duplicate_requests_cannot_create_v4(session, monkeypatch):
    review_id = await _historical_v2_without_snapshot(session, monkeypatch)
    first = await _fresh_matching_snapshot(session, review_id)
    replay = await _fresh_matching_snapshot(session, review_id)
    assert first == replay  # existing compare-and-confirm contract
    assert session.scalar(select(WorkbenchReviewItem)).version == 3
    assert session.query(WorkbenchReviewReclassification).count() == 2
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 1
    # Even a caller deliberately requesting the new version cannot force unchanged output.
    unchanged = await _fresh_matching_snapshot(session, review_id, expected_version=3)
    assert unchanged.changed is False and unchanged.to_version == 3
    with pytest.raises(ReviewVersionConflictError):
        await _fresh_matching_snapshot(session, review_id, expected_version=99)


async def test_08o_conflicting_duplicate_fails_closed(session, monkeypatch):
    review_id = await _historical_v2_without_snapshot(session, monkeypatch)
    await _fresh_matching_snapshot(session, review_id)
    changed = replace(_resolved_facts(_invoice()), partner_match=_partner(PartnerMatchStatus.NOT_FOUND))
    with pytest.raises(ReviewVersionConflictError):
        await _fresh_matching_snapshot(session, review_id, facts=changed)
    assert session.scalar(select(WorkbenchReviewItem)).version == 3
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 1


async def test_08o_snapshot_failure_rolls_back_new_version(session, monkeypatch):
    review_id = await _historical_v2_without_snapshot(session, monkeypatch)
    original_flush = session.flush

    def fail_snapshot(objects=None):
        if any(isinstance(row, WorkbenchReviewExecutionEvidence) for row in session.new):
            raise RuntimeError("Fixture persistence failure")
        return original_flush(objects)

    monkeypatch.setattr(session, "flush", fail_snapshot)
    with pytest.raises(RuntimeError, match="Fixture persistence failure"):
        await _fresh_matching_snapshot(session, review_id)
    session.rollback()
    assert session.scalar(select(WorkbenchReviewItem)).version == 2
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 0
    assert session.query(WorkbenchReviewReclassification).count() == 1


async def test_08o_compare_and_set_rejects_version_race_before_snapshot_insert(session, monkeypatch):
    review_id = await _historical_v2_without_snapshot(session, monkeypatch)
    repository = SqlAlchemyReviewRepository(session)
    original = repository._find_review_execution_evidence

    def competing_transition(**kwargs):
        # Simulate another actor winning between the version read and the CAS.
        from sqlalchemy import update

        result = original(**kwargs)
        session.execute(update(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_id).values(version=3))
        return result

    monkeypatch.setattr(repository, "_find_review_execution_evidence", competing_transition)
    with pytest.raises(ReviewVersionConflictError):
        await ReclassifyWorkbenchReviewUseCase(
            decision_engine=_decision_engine(_resolved_facts(_invoice())),
            source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
            reclassification_writer=repository,
        ).execute(
            ReclassifyReviewCommand(
                review_id=review_id,
                company_id=COMPANY_ID,
                expected_version=2,
                trigger=ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
                note="Explicit current-time refresh",
            )
        )
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 0
    assert session.query(WorkbenchReviewReclassification).count() == 1
    session.rollback()
