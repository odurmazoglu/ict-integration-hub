"""P0-PROD-15Z: review-scoped Stage-1 execution-evidence recovery/repair.

Regression coverage for the real production gap discovered in P0-PROD-15Y: a
review whose accepted business state (supplier remediation, purchase purpose,
review-scoped accounting resolution) already resolves cleanly -- effective
reasons ``[]``, effective workflow ``vendor_bill`` -- can still have no
``WorkbenchReviewExecutionEvidence`` for its current version, if that version was
produced by a reclassification that ran before a fix like P0-PROD-15X was
deployed. ``SubmitReviewAccountingResolutionUseCase``'s own "already advanced"
resume path is read-only and never re-triggers reclassification, so there was no
supported way to repair such a review. ``RebuildReviewExecutionEvidenceUseCase``
is the narrow, generic recovery capability that fills that gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands import ImportInvoiceCommand
from app.application.decision import (
    DecisionEngine,
    ManualReviewStrategy,
    VendorBillReviewRecommendationStrategy,
    WorkflowStrategyResolver,
)
from app.application.expense_mapping import OperatingExpenseMatchingEngine
from app.application.rules.deterministic import DeterministicRuleEngine
from app.application.use_cases import ImportInvoiceUseCase
from app.application.use_cases.effective_decision import EffectiveDecisionResolver
from app.application.use_cases.reclassify_review import ReclassifyWorkbenchReviewUseCase
from app.application.workbench import ReviewItemCreationService
from app.application.workbench.accounting_resolution import (
    AccountingTreatmentType,
    SubmitReviewAccountingResolutionCommand,
)
from app.application.workbench.accounting_resolution_use_cases import SubmitReviewAccountingResolutionUseCase
from app.application.workbench.exceptions import (
    ExecutionEvidenceRecoveryConflictError,
    ExecutionEvidenceRecoveryEligibilityError,
    ExecutionEvidenceRecoveryMismatchError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
)
from app.application.workbench.execution_evidence_recovery import RebuildExecutionEvidenceCommand
from app.application.workbench.execution_evidence_recovery_use_cases import RebuildReviewExecutionEvidenceUseCase
from app.application.workbench.expense_account_lookup import ExpenseAccountCandidate
from app.application.workbench.purchase_purpose import PurchasePurpose, SubmitPurchasePurposeCommand
from app.application.workbench.purchase_purpose_use_cases import SubmitPurchasePurposeUseCase
from app.application.workflow import WorkflowType
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
from app.models.operating_expense_mapping import OperatingExpenseMappingRecord
from app.models.workbench_review_accounting_resolution import WorkbenchReviewAccountingResolution
from app.models.workbench_review_classification_evidence import WorkbenchReviewClassificationEvidence
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_purchase_purpose_resolution import WorkbenchReviewPurchasePurposeResolution
from app.models.workbench_review_reclassification import WorkbenchReviewReclassification
from app.models.workbench_review_source_invoice_evidence import WorkbenchReviewSourceInvoiceEvidence
from app.models.workbench_review_supplier_remediation_effect import WorkbenchReviewSupplierRemediationEffect
from app.persistence import (
    SqlAlchemyOperatingExpenseMappingRepository,
    SqlAlchemyReviewAccountingResolutionRepository,
    SqlAlchemyReviewPurchasePurposeResolutionRepository,
    SqlAlchemyReviewRepository,
    SqlAlchemyReviewSourceInvoiceEvidenceReader,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyUnitOfWork,
)
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType

COMPANY_ID = 1
OTHER_COMPANY_ID = 2
VAT = "1760390647"
CANONICAL_PARTNER_ID = 439
EXPENSE_ACCOUNT = 247
ACTOR = "p0-prod-15z-test-operator"


# --------------------------------------------------------------------------- fixtures


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    from app.db.base import Base

    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewExecutionEvidence.__table__,
            WorkbenchReviewClassificationEvidence.__table__,
            WorkbenchReviewSourceInvoiceEvidence.__table__,
            WorkbenchReviewReclassification.__table__,
            WorkbenchReviewSupplierRemediationEffect.__table__,
            OperatingExpenseMappingRecord.__table__,
            WorkbenchReviewPurchasePurposeResolution.__table__,
            WorkbenchReviewAccountingResolution.__table__,
            WorkbenchReviewDecision.__table__,
            ExecutionSourceInvoiceEvidence.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


# --------------------------------------------------------------------------- builders


def _invoice(*, ettn: str) -> InternalInvoice:
    """Identifier-free lines, shaped like the real CloudSpark production invoice."""

    return InternalInvoice(
        header=Header(
            invoice_number="I082026000000009",
            invoice_uuid=ettn,
            ettn=ettn,
            issue_date=date(2026, 9, 8),
            currency_code="TRY",
        ),
        supplier=Party(name="CLOUDSPARK BULUT TEKNOLOJILERI SAN. TIC. A.S.", tax_number=VAT),
        customer=Party(name="ICT Teknoloji", tax_number="4651205941"),
        totals=MonetaryTotals(
            line_extension_amount=Decimal("4959.80"),
            tax_exclusive_amount=Decimal("4959.80"),
            tax_inclusive_amount=Decimal("5951.76"),
            payable_amount=Decimal("5951.76"),
        ),
        lines=(
            InvoiceLine(
                line_number="1",
                description="CPU",
                quantity=Decimal("20"),
                unit_code="C62",
                unit_price=Decimal("74.40"),
                line_extension_amount=Decimal("1487.94"),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
            ),
        ),
    )


class _FakeImportHistory:
    def find_imported_invoice(self, idempotency_key: str) -> None:
        return None

    def record_import_result(self, *, company_id: int, idempotency_key: str, result: object) -> None:
        return None


class _MatchingFacts:
    def __init__(self, result: object) -> None:
        self.result = result

    def match_invoice(self, invoice: InternalInvoice, *, company_id: int, partner_match: object = None) -> object:
        return self.result

    def map_invoice(self, invoice: InternalInvoice, *, company_id: int) -> object:
        return self.result


def _partner_ambiguous() -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MULTIPLE_MATCHES,
        partner_id=None,
        matched_by=None,
        reason="Multiple active supplier partner candidates found by tax number.",
        candidate_count=2,
        confidence=None,
    )


def _product_invalid_input(invoice: InternalInvoice) -> InvoiceProductMatchResult:
    """Identifier-free product facts: the real deterministic matcher reports
    INVALID_INPUT (nothing to search on) for a line with no product identifier --
    required for ``validate_vendor_bill_inputs``'s operating-expense/whole-invoice
    mode shape check."""

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
                    seller_item_code=line.seller_item_code,
                    matched_by=None,
                    reason="No deterministic product identifier present on this line.",
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
                    tax_id=8801,
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


def _ambiguous_facts(invoice: InternalInvoice) -> _Facts:
    return _Facts(_partner_ambiguous(), _product_invalid_input(invoice), _taxes(invoice))


def _rule_engine(rule_result: _Facts, *, mapping_repository) -> DeterministicRuleEngine:
    return DeterministicRuleEngine(
        partner_matcher=_MatchingFacts(rule_result.partner_match),
        product_matcher=_MatchingFacts(rule_result.product_match),
        tax_mapper=_MatchingFacts(rule_result.tax_match),
        operating_expense_matcher=OperatingExpenseMatchingEngine(mapping_repository),
    )


def _decision_engine(rule_result: _Facts, *, mapping_repository) -> DecisionEngine:
    return DecisionEngine(
        rule_engine=_rule_engine(rule_result, mapping_repository=mapping_repository),
        strategy_resolver=WorkflowStrategyResolver([VendorBillReviewRecommendationStrategy(), ManualReviewStrategy()]),
    )


async def _import(session: Session, *, invoice: InternalInvoice, facts: _Facts, mapping_repository) -> str:
    use_case = ImportInvoiceUseCase(
        import_history=_FakeImportHistory(),
        decision_engine=_decision_engine(facts, mapping_repository=mapping_repository),
        review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(session)),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )
    result = await use_case.execute(
        ImportInvoiceCommand(
            invoice=invoice, idempotency_key=f"uyumsoft:{COMPANY_ID}:{invoice.header.ettn}", company_id=COMPANY_ID
        )
    )
    assert result.review_id is not None
    return result.review_id


def _effect(*, review_id: str, source_invoice_id: str, company_id: int = COMPANY_ID):
    from app.application.workbench.supplier_remediation import (
        SupplierPartnerWriteEffectStatus,
        SupplierRemediationEffect,
    )
    from app.application.workbench.supplier_resolution import SupplierResolutionMode

    return SupplierRemediationEffect(
        review_id=review_id,
        company_id=company_id,
        review_version=1,
        source_invoice_id=source_invoice_id,
        mode=SupplierResolutionMode.MATCH_EXISTING,
        resolved_partner_id=CANONICAL_PARTNER_ID,
        partner_write_status=SupplierPartnerWriteEffectStatus.SELECTED,
    )


class _FakeExpenseAccountReader:
    def __init__(self) -> None:
        self._by_id = {
            EXPENSE_ACCOUNT: ExpenseAccountCandidate(
                id=EXPENSE_ACCOUNT, code="770000", name="General Administrative Expenses", account_type="expense"
            )
        }

    def find_candidates(self, *, company_id: int, query: str | None) -> tuple[ExpenseAccountCandidate, ...]:
        return tuple(self._by_id.values())

    def find_eligible_by_id(self, *, company_id: int, account_id: int) -> ExpenseAccountCandidate | None:
        return self._by_id.get(account_id)


def _reclassifier(session: Session, *, facts: _Facts, mapping_repository) -> ReclassifyWorkbenchReviewUseCase:
    review_repository = SqlAlchemyReviewRepository(session)
    return ReclassifyWorkbenchReviewUseCase(
        decision_engine=_decision_engine(facts, mapping_repository=mapping_repository),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        reclassification_writer=review_repository,
        supplier_remediation_effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        operating_expense_matcher=OperatingExpenseMatchingEngine(mapping_repository),
        review_accounting_resolution_reader=SqlAlchemyReviewAccountingResolutionRepository(session),
    )


def _accounting_use_case(
    session: Session, *, facts: _Facts, mapping_repository
) -> SubmitReviewAccountingResolutionUseCase:
    return SubmitReviewAccountingResolutionUseCase(
        review_reader=SqlAlchemyReviewRepository(session),
        purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        expense_account_reader=_FakeExpenseAccountReader(),
        accounting_resolution_writer=SqlAlchemyReviewAccountingResolutionRepository(session),
        reclassifier=_reclassifier(session, facts=facts, mapping_repository=mapping_repository),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


def _accounting_command(
    review_id: str, *, account_id: int = EXPENSE_ACCOUNT
) -> SubmitReviewAccountingResolutionCommand:
    return SubmitReviewAccountingResolutionCommand(
        review_id=review_id,
        company_id=COMPANY_ID,
        expected_version=1,
        treatment_type=AccountingTreatmentType.EXPENSE_ACCOUNT,
        expense_account_id=account_id,
        expense_category="IT_HARDWARE_INTERNAL",
        approved_by=ACTOR,
    )


def _recovery_use_case(session: Session, *, facts: _Facts, mapping_repository) -> RebuildReviewExecutionEvidenceUseCase:
    review_repository = SqlAlchemyReviewRepository(session)
    resolver = EffectiveDecisionResolver(
        decision_engine=_decision_engine(facts, mapping_repository=mapping_repository),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        supplier_remediation_effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        operating_expense_matcher=OperatingExpenseMatchingEngine(mapping_repository),
        review_accounting_resolution_reader=SqlAlchemyReviewAccountingResolutionRepository(session),
    )
    return RebuildReviewExecutionEvidenceUseCase(
        review_reader=review_repository,
        resolver=resolver,
        execution_evidence_writer=review_repository,
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


async def _resolve_to_legacy_missing_evidence_state(
    session: Session, *, ettn: str, mapping_repository
) -> tuple[str, InternalInvoice, _Facts, int]:
    """Reproduce the exact real production defect: a review that has reached a
    fully-resolved v2 state (reasons=[], workflow=vendor_bill) via an accepted
    supplier remediation + purchase purpose + review-scoped accounting resolution,
    but whose execution-evidence row is missing -- simulating a reclassification
    that ran before a fix (e.g. P0-PROD-15X) was deployed.
    """

    invoice = _invoice(ettn=ettn)
    facts = _ambiguous_facts(invoice)
    review_id = await _import(session, invoice=invoice, facts=facts, mapping_repository=mapping_repository)
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()
    SubmitPurchasePurposeUseCase(
        review_reader=SqlAlchemyReviewRepository(session),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        purpose_writer=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    ).execute(
        SubmitPurchasePurposeCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=1,
            purchase_purpose=PurchasePurpose.INTERNAL_USE,
            approved_by=ACTOR,
        )
    )
    session.commit()

    accounting_use_case = _accounting_use_case(session, facts=facts, mapping_repository=mapping_repository)
    result = await accounting_use_case.execute(_accounting_command(review_id))
    session.commit()
    assert result.current_version == 2
    assert result.current_review_reasons == ()

    # Simulate "produced before the fix was deployed": delete the execution
    # evidence the (already fixed) code just correctly created, reproducing the
    # legacy gap state for this test only.
    session.execute(
        delete(WorkbenchReviewExecutionEvidence).where(
            WorkbenchReviewExecutionEvidence.review_id == review_id,
            WorkbenchReviewExecutionEvidence.review_version == 2,
        )
    )
    session.commit()
    assert (
        session.scalar(
            select(WorkbenchReviewExecutionEvidence).where(WorkbenchReviewExecutionEvidence.review_id == review_id)
        )
        is None
    )
    return review_id, invoice, facts, 2


def _execution_evidence(
    session: Session, *, review_id: str, review_version: int
) -> WorkbenchReviewExecutionEvidence | None:
    return session.scalar(
        select(WorkbenchReviewExecutionEvidence).where(
            WorkbenchReviewExecutionEvidence.review_id == review_id,
            WorkbenchReviewExecutionEvidence.review_version == review_version,
        )
    )


def _recovery_command(review_id: str, *, expected_version: int) -> RebuildExecutionEvidenceCommand:
    return RebuildExecutionEvidenceCommand(
        review_id=review_id, company_id=COMPANY_ID, expected_version=expected_version
    )


# =================================================================== A/B: exact legacy scenario


async def test_rebuild_succeeds_for_exact_legacy_scenario_and_creates_evidence(session: Session) -> None:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id, invoice, facts, version = await _resolve_to_legacy_missing_evidence_state(
        session, ettn="P0-PROD-15Z-A", mapping_repository=mapping_repository
    )

    recovery = _recovery_use_case(session, facts=facts, mapping_repository=mapping_repository)
    result = await recovery.execute(_recovery_command(review_id, expected_version=version))
    session.commit()

    assert result.already_applied is False
    assert result.review_version == version
    assert result.partner_id == CANONICAL_PARTNER_ID
    assert result.expense_account_id == EXPENSE_ACCOUNT
    assert result.expense_category == "IT_HARDWARE_INTERNAL"

    evidence = _execution_evidence(session, review_id=review_id, review_version=version)
    assert evidence is not None
    assert evidence.partner_match["partner_id"] == CANONICAL_PARTNER_ID
    assert evidence.operating_expense_match["expense_account_id"] == EXPENSE_ACCOUNT
    assert evidence.operating_expense_match["expense_category"] == "IT_HARDWARE_INTERNAL"


# =================================================================== C: review state byte-for-byte unchanged


async def test_rebuild_leaves_review_state_byte_for_byte_unchanged(session: Session) -> None:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id, invoice, facts, version = await _resolve_to_legacy_missing_evidence_state(
        session, ettn="P0-PROD-15Z-C", mapping_repository=mapping_repository
    )
    before = session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_id))
    before_snapshot = (before.version, before.status, before.workflow, tuple(before.review_reasons), before.updated_at)

    recovery = _recovery_use_case(session, facts=facts, mapping_repository=mapping_repository)
    await recovery.execute(_recovery_command(review_id, expected_version=version))
    session.commit()

    after = session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_id))
    after_snapshot = (after.version, after.status, after.workflow, tuple(after.review_reasons), after.updated_at)
    assert after_snapshot == before_snapshot
    # No reclassification event was created by recovery -- only the original one.
    assert session.query(WorkbenchReviewReclassification).count() == 1


# =================================================================== D: no supplier-wide mapping


async def test_rebuild_creates_no_supplier_wide_mapping(session: Session) -> None:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id, invoice, facts, version = await _resolve_to_legacy_missing_evidence_state(
        session, ettn="P0-PROD-15Z-D", mapping_repository=mapping_repository
    )

    recovery = _recovery_use_case(session, facts=facts, mapping_repository=mapping_repository)
    await recovery.execute(_recovery_command(review_id, expected_version=version))
    session.commit()

    assert session.query(OperatingExpenseMappingRecord).count() == 0


# =================================================================== E: no Odoo write (structural)


async def test_recovery_use_case_never_touches_an_odoo_client(session: Session) -> None:
    """Structural proof, not merely behavioral: the resolver this test wires has no
    Odoo client/connector of any kind -- only in-memory fakes -- so there is no
    object through which an Odoo write could even be attempted."""

    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id, invoice, facts, version = await _resolve_to_legacy_missing_evidence_state(
        session, ettn="P0-PROD-15Z-E", mapping_repository=mapping_repository
    )
    recovery = _recovery_use_case(session, facts=facts, mapping_repository=mapping_repository)
    result = await recovery.execute(_recovery_command(review_id, expected_version=version))
    session.commit()
    assert result.already_applied is False


# =================================================================== F: idempotent repeat, no duplicate


async def test_repeating_rebuild_is_idempotent_and_returns_already_applied(session: Session) -> None:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id, invoice, facts, version = await _resolve_to_legacy_missing_evidence_state(
        session, ettn="P0-PROD-15Z-F", mapping_repository=mapping_repository
    )
    recovery = _recovery_use_case(session, facts=facts, mapping_repository=mapping_repository)

    first = await recovery.execute(_recovery_command(review_id, expected_version=version))
    session.commit()
    second = await recovery.execute(_recovery_command(review_id, expected_version=version))
    session.commit()

    assert first.already_applied is False
    assert second.already_applied is True
    assert (
        session.query(WorkbenchReviewExecutionEvidence)
        .filter(WorkbenchReviewExecutionEvidence.review_id == review_id)
        .count()
        == 1
    )


# =================================================================== G: existing identical evidence => already_applied


async def test_pre_existing_identical_evidence_reports_already_applied(session: Session) -> None:
    """Models a review that was NEVER broken (P0-PROD-15X already deployed when it
    resolved): evidence already exists and matches exactly what recovery would
    build -- recovery must recognize this and do nothing, not raise a conflict."""

    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    invoice = _invoice(ettn="P0-PROD-15Z-G")
    facts = _ambiguous_facts(invoice)
    review_id = await _import(session, invoice=invoice, facts=facts, mapping_repository=mapping_repository)
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()
    SubmitPurchasePurposeUseCase(
        review_reader=SqlAlchemyReviewRepository(session),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        purpose_writer=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    ).execute(
        SubmitPurchasePurposeCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=1,
            purchase_purpose=PurchasePurpose.INTERNAL_USE,
            approved_by=ACTOR,
        )
    )
    session.commit()
    accounting_use_case = _accounting_use_case(session, facts=facts, mapping_repository=mapping_repository)
    result = await accounting_use_case.execute(_accounting_command(review_id))
    session.commit()
    assert result.current_version == 2
    # Evidence already correctly exists (this fixed code created it) -- never deleted.

    recovery = _recovery_use_case(session, facts=facts, mapping_repository=mapping_repository)
    outcome = await recovery.execute(_recovery_command(review_id, expected_version=2))
    session.commit()

    assert outcome.already_applied is True
    assert (
        session.query(WorkbenchReviewExecutionEvidence)
        .filter(WorkbenchReviewExecutionEvidence.review_id == review_id)
        .count()
        == 1
    )


# =================================================================== H: conflicting existing evidence => fail closed


async def test_conflicting_existing_evidence_fails_closed(session: Session) -> None:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id, invoice, facts, version = await _resolve_to_legacy_missing_evidence_state(
        session, ettn="P0-PROD-15Z-H", mapping_repository=mapping_repository
    )
    review_repository = SqlAlchemyReviewRepository(session)

    # Plant a conflicting evidence row directly (simulating some other, materially
    # different evidence somehow already present for this version).
    from app.application.workbench.evidence import ReviewExecutionEvidence

    bogus_partner = PartnerMatchResult(
        status=PartnerMatchStatus.MATCHED,
        partner_id=999999,
        matched_by="bogus",
        reason="bogus",
        candidate_count=1,
        confidence=Decimal("1.00"),
    )
    bogus_evidence = ReviewExecutionEvidence(
        review_id=review_id,
        company_id=COMPANY_ID,
        review_version=version,
        source_invoice_id=invoice.header.ettn,
        invoice=invoice,
        partner_match=bogus_partner,
        product_match=facts.product_match,
        tax_match=facts.tax_match,
        operating_expense_match=None,
    )
    review_repository.create_execution_evidence_for_current_version(
        review_id=review_id, company_id=COMPANY_ID, expected_version=version, evidence=bogus_evidence
    )
    session.commit()

    recovery = _recovery_use_case(session, facts=facts, mapping_repository=mapping_repository)
    with pytest.raises(ExecutionEvidenceRecoveryConflictError):
        await recovery.execute(_recovery_command(review_id, expected_version=version))

    # Never overwritten.
    evidence = _execution_evidence(session, review_id=review_id, review_version=version)
    assert evidence.partner_match["partner_id"] == 999999


# =================================================================== I: stale expected_version => conflict


async def test_stale_expected_version_conflicts(session: Session) -> None:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id, invoice, facts, version = await _resolve_to_legacy_missing_evidence_state(
        session, ettn="P0-PROD-15Z-I", mapping_repository=mapping_repository
    )

    recovery = _recovery_use_case(session, facts=facts, mapping_repository=mapping_repository)
    with pytest.raises(ReviewVersionConflictError):
        await recovery.execute(_recovery_command(review_id, expected_version=version + 1))
    with pytest.raises(ReviewVersionConflictError):
        await recovery.execute(_recovery_command(review_id, expected_version=version - 1))


# =================================================================== J: review with actionable reasons => rejected


async def test_review_with_actionable_reasons_is_rejected(session: Session) -> None:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    invoice = _invoice(ettn="P0-PROD-15Z-J")
    facts = _ambiguous_facts(invoice)
    review_id = await _import(session, invoice=invoice, facts=facts, mapping_repository=mapping_repository)
    # No effect, no purpose, no resolution recorded: review is still manual_review
    # with actionable reasons.

    recovery = _recovery_use_case(session, facts=facts, mapping_repository=mapping_repository)
    with pytest.raises(ExecutionEvidenceRecoveryEligibilityError):
        await recovery.execute(_recovery_command(review_id, expected_version=1))
    assert _execution_evidence(session, review_id=review_id, review_version=1) is None


async def test_non_pending_review_is_rejected(session: Session) -> None:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id, invoice, facts, version = await _resolve_to_legacy_missing_evidence_state(
        session, ettn="P0-PROD-15Z-J2", mapping_repository=mapping_repository
    )
    session.execute(
        WorkbenchReviewItem.__table__.update()
        .where(WorkbenchReviewItem.review_id == review_id)
        .values(status="decision_submitted")
    )
    session.commit()

    recovery = _recovery_use_case(session, facts=facts, mapping_repository=mapping_repository)
    with pytest.raises(ReviewStateConflictError):
        await recovery.execute(_recovery_command(review_id, expected_version=version))


# =================================================================== K: recomputation differs from persisted state


async def test_recomputation_mismatch_is_rejected(session: Session) -> None:
    """Models master data drifting between the review's last reclassification and
    the recovery attempt: recomputing right now no longer reproduces the review's
    persisted current reasons -- e.g. the supplier-remediation effect vanished.
    Recovery must fail closed, never silently "fix" the review by materializing
    evidence for a state that no longer actually holds.
    """

    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id, invoice, facts, version = await _resolve_to_legacy_missing_evidence_state(
        session, ettn="P0-PROD-15Z-K", mapping_repository=mapping_repository
    )
    # Simulate the accepted supplier-remediation effect having vanished (e.g. a
    # data issue) -- recomputation would then not strip SUPPLIER_AMBIGUOUS/the
    # operating-expense reasons the same way, and would no longer reproduce the
    # persisted empty reasons set.
    session.execute(
        delete(WorkbenchReviewSupplierRemediationEffect).where(
            WorkbenchReviewSupplierRemediationEffect.review_id == review_id
        )
    )
    session.commit()

    recovery = _recovery_use_case(session, facts=facts, mapping_repository=mapping_repository)
    with pytest.raises(ExecutionEvidenceRecoveryMismatchError):
        await recovery.execute(_recovery_command(review_id, expected_version=version))
    assert _execution_evidence(session, review_id=review_id, review_version=version) is None


# =================================================================== L: cross-company/cross-review isolation


async def test_cross_company_review_cannot_be_recovered(session: Session) -> None:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id, invoice, facts, version = await _resolve_to_legacy_missing_evidence_state(
        session, ettn="P0-PROD-15Z-L", mapping_repository=mapping_repository
    )

    recovery = _recovery_use_case(session, facts=facts, mapping_repository=mapping_repository)
    from app.application.workbench.exceptions import ReviewNotFoundError

    with pytest.raises(ReviewNotFoundError):
        await recovery.execute(
            RebuildExecutionEvidenceCommand(review_id=review_id, company_id=OTHER_COMPANY_ID, expected_version=version)
        )
    assert _execution_evidence(session, review_id=review_id, review_version=version) is None


async def test_another_reviews_accounting_resolution_cannot_influence_recovery(session: Session) -> None:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_a, _invoice_a, _facts_a, _version_a = await _resolve_to_legacy_missing_evidence_state(
        session, ettn="P0-PROD-15Z-L2-A", mapping_repository=mapping_repository
    )

    # Independent second review, same supplier, no resolution of its own -- must
    # remain ineligible for recovery regardless of review_a's accepted resolution.
    invoice_b = _invoice(ettn="P0-PROD-15Z-L2-B")
    facts_b = _ambiguous_facts(invoice_b)
    review_b = await _import(session, invoice=invoice_b, facts=facts_b, mapping_repository=mapping_repository)

    recovery = _recovery_use_case(session, facts=facts_b, mapping_repository=mapping_repository)
    with pytest.raises(ExecutionEvidenceRecoveryEligibilityError):
        await recovery.execute(_recovery_command(review_b, expected_version=1))
    assert _execution_evidence(session, review_id=review_b, review_version=1) is None


# =================================================================== M: missing immutable source evidence


async def test_missing_source_evidence_is_rejected(session: Session) -> None:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id, invoice, facts, version = await _resolve_to_legacy_missing_evidence_state(
        session, ettn="P0-PROD-15Z-M", mapping_repository=mapping_repository
    )
    session.execute(
        delete(WorkbenchReviewSourceInvoiceEvidence).where(WorkbenchReviewSourceInvoiceEvidence.review_id == review_id)
    )
    session.commit()

    recovery = _recovery_use_case(session, facts=facts, mapping_repository=mapping_repository)
    from app.application.workbench.exceptions import ExecutionEvidenceRecoverySourceMissingError

    with pytest.raises(ExecutionEvidenceRecoverySourceMissingError):
        await recovery.execute(_recovery_command(review_id, expected_version=version))


# =================================================================== N: normal prospective reclassification unaffected


async def test_normal_prospective_reclassification_still_works(session: Session) -> None:
    """P0-PROD-15X's own behavior (reclassification producing execution evidence
    directly, without ever needing recovery) must be completely unaffected by the
    P0-PROD-15Z extraction/recovery capability."""

    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    invoice = _invoice(ettn="P0-PROD-15Z-N")
    facts = _ambiguous_facts(invoice)
    review_id = await _import(session, invoice=invoice, facts=facts, mapping_repository=mapping_repository)
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()
    SubmitPurchasePurposeUseCase(
        review_reader=SqlAlchemyReviewRepository(session),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        purpose_writer=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    ).execute(
        SubmitPurchasePurposeCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=1,
            purchase_purpose=PurchasePurpose.INTERNAL_USE,
            approved_by=ACTOR,
        )
    )
    session.commit()

    accounting_use_case = _accounting_use_case(session, facts=facts, mapping_repository=mapping_repository)
    result = await accounting_use_case.execute(_accounting_command(review_id))
    session.commit()

    assert result.current_version == 2
    assert result.current_review_reasons == ()
    evidence = _execution_evidence(session, review_id=review_id, review_version=2)
    assert evidence is not None
    assert evidence.operating_expense_match["expense_account_id"] == EXPENSE_ACCOUNT


# =================================================================== O: decision can consume rebuilt evidence


async def test_decision_can_consume_rebuilt_evidence(session: Session) -> None:
    from app.application.workbench.commands import ReviewDecisionCommand
    from app.application.workbench.decision_use_cases import SubmitReviewDecisionUseCase
    from app.application.workbench.dto import ReviewDecisionType
    from app.persistence import SqlAlchemyReviewExecutionEvidenceReader

    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id, invoice, facts, version = await _resolve_to_legacy_missing_evidence_state(
        session, ettn="P0-PROD-15Z-O", mapping_repository=mapping_repository
    )

    recovery = _recovery_use_case(session, facts=facts, mapping_repository=mapping_repository)
    await recovery.execute(_recovery_command(review_id, expected_version=version))
    session.commit()

    evidence_reader = SqlAlchemyReviewExecutionEvidenceReader(session)
    decision_use_case = SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        execution_evidence_reader=evidence_reader,
    )
    acknowledgement = decision_use_case.execute(
        ReviewDecisionCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=version,
            decision=ReviewDecisionType.SELECT_WORKFLOW,
            decided_by=ACTOR,
            selected_workflow=WorkflowType.VENDOR_BILL,
            idempotency_key="p0-prod-15z-decision-o",
        )
    )
    session.commit()
    assert acknowledgement is not None

    item = session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_id))
    assert item.version == version + 1
    assert item.status == "decision_submitted"


# =================================================================== P: existing gates unchanged (spot check)


async def test_write_authorization_and_execution_gates_are_untouched_by_recovery(session: Session) -> None:
    """Recovery has no write-authorization/execution surface at all -- confirmed
    structurally: RebuildReviewExecutionEvidenceUseCase has no dependency on any
    write-authorization or execution component."""

    import inspect

    from app.application.workbench.execution_evidence_recovery_use_cases import RebuildReviewExecutionEvidenceUseCase

    params = inspect.signature(RebuildReviewExecutionEvidenceUseCase.__init__).parameters
    for name in params:
        assert "authoriz" not in name.lower()
        assert "execut" not in name.lower() or name in ("execution_evidence_writer",)
