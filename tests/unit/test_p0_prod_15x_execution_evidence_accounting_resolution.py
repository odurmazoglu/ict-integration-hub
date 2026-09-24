"""P0-PROD-15X: review-scoped accounting-resolution execution-evidence gap.

Regression coverage for the real production failure discovered resuming the
CloudSpark invoice (P0-PROD-15W): ``ReclassifyWorkbenchReviewUseCase`` correctly
folded an accepted, review-scoped ``ReviewAccountingResolution`` (P0-PROD-15T)
into the review's *effective* reasons/workflow, but never into the Stage-1
``WorkbenchReviewExecutionEvidence`` it builds and persists -- so a review that
looked fully resolved (``[]`` reasons, ``vendor_bill`` workflow) could never
actually reach a submittable decision: ``SubmitReviewDecisionUseCase`` requires
an execution-evidence row for the review's current version, and none was ever
written, so decision submission failed with ``execution_source_invoice_not_found``.

The fix threads the same ``recomputed_operating_expense_match`` already used for
effective reasons/workflow into ``_execution_decision_result`` -- one authoritative
effective result, never independently recomputed for execution evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
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
from app.application.expense_mapping import OperatingExpenseMatchingEngine
from app.application.rules.deterministic import DeterministicRuleEngine
from app.application.use_cases import ImportInvoiceUseCase
from app.application.use_cases.reclassify_review import ReclassifyWorkbenchReviewUseCase
from app.application.workbench import ReviewItemCreationService
from app.application.workbench.accounting_resolution import (
    AccountingTreatmentType,
    SubmitReviewAccountingResolutionCommand,
)
from app.application.workbench.accounting_resolution_use_cases import SubmitReviewAccountingResolutionUseCase
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.decision_use_cases import SubmitReviewDecisionUseCase
from app.application.workbench.dto import ReviewDecisionType
from app.application.workbench.expense_account_lookup import ExpenseAccountCandidate
from app.application.workbench.purchase_purpose import PurchasePurpose, SubmitPurchasePurposeCommand
from app.application.workbench.purchase_purpose_use_cases import SubmitPurchasePurposeUseCase
from app.application.workbench.reclassification import ReclassifyReviewCommand, ReviewReclassificationTrigger
from app.application.workbench.supplier_remediation import SupplierPartnerWriteEffectStatus, SupplierRemediationEffect
from app.application.workbench.supplier_resolution import SupplierResolutionMode
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
    SqlAlchemyReviewExecutionEvidenceReader,
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
SUPPLIER_WIDE_ACCOUNT = 193
TAX_ID = 8801
ACTOR = "p0-prod-15x-test-operator"


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


# --------------------------------------------------------------------------- builders (mirrors test_p0_prod_15t)


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

    def match_invoice(self, invoice: InternalInvoice, *, company_id: int) -> object:
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


def _product_not_found(invoice: InternalInvoice) -> InvoiceProductMatchResult:
    """Product match facts for the (identifier-free) fixture invoice.

    Real production identifier-free lines carry no buyer/seller item code or
    barcode, so the real deterministic product matcher reports ``INVALID_INPUT``
    (nothing to search on) rather than ``NOT_FOUND`` -- and
    ``validate_vendor_bill_inputs``'s operating-expense/whole-invoice mode
    specifically requires that shape (see
    ``_operating_expense_product_shape_errors`` in ``app.billing.builder``).
    """

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


def _ambiguous_facts(invoice: InternalInvoice) -> _Facts:
    return _Facts(_partner_ambiguous(), _product_not_found(invoice), _taxes(invoice))


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


def _effect(
    *,
    review_id: str,
    source_invoice_id: str,
    company_id: int = COMPANY_ID,
    partner_id: int = CANONICAL_PARTNER_ID,
):
    return SupplierRemediationEffect(
        review_id=review_id,
        company_id=company_id,
        review_version=1,
        source_invoice_id=source_invoice_id,
        mode=SupplierResolutionMode.MATCH_EXISTING,
        resolved_partner_id=partner_id,
        partner_write_status=SupplierPartnerWriteEffectStatus.SELECTED,
    )


def _account(*, id_: int) -> ExpenseAccountCandidate:
    return ExpenseAccountCandidate(
        id=id_, code="770000", name="General Administrative Expenses", account_type="expense"
    )


class _FakeExpenseAccountReader:
    def __init__(self) -> None:
        self._by_id = {EXPENSE_ACCOUNT: _account(id_=EXPENSE_ACCOUNT)}

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


async def _resolve_review(
    session: Session,
    *,
    ettn: str,
    mapping_repository,
    create_effect: bool = True,
) -> tuple[str, InternalInvoice, _Facts]:
    """Import + (optionally) remediate supplier + record INTERNAL_USE purpose.

    Stops short of the accounting-resolution submission so callers can vary it.
    """

    invoice = _invoice(ettn=ettn)
    facts = _ambiguous_facts(invoice)
    review_id = await _import(session, invoice=invoice, facts=facts, mapping_repository=mapping_repository)
    if create_effect:
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
    return review_id, invoice, facts


def _execution_evidence(
    session: Session, *, review_id: str, review_version: int
) -> WorkbenchReviewExecutionEvidence | None:
    return session.scalar(
        select(WorkbenchReviewExecutionEvidence).where(
            WorkbenchReviewExecutionEvidence.review_id == review_id,
            WorkbenchReviewExecutionEvidence.review_version == review_version,
        )
    )


def _classification_evidence(
    session: Session, *, review_id: str, review_version: int
) -> WorkbenchReviewClassificationEvidence | None:
    return session.scalar(
        select(WorkbenchReviewClassificationEvidence).where(
            WorkbenchReviewClassificationEvidence.review_id == review_id,
            WorkbenchReviewClassificationEvidence.review_version == review_version,
        )
    )


def _reason_codes_from_payload(payload: list[dict]) -> set[str]:
    return {entry["code"] for entry in payload}


def _accounting_command(review_id: str, *, account_id: int) -> SubmitReviewAccountingResolutionCommand:
    return SubmitReviewAccountingResolutionCommand(
        review_id=review_id,
        company_id=COMPANY_ID,
        expected_version=1,
        treatment_type=AccountingTreatmentType.EXPENSE_ACCOUNT,
        expense_account_id=account_id,
        expense_category="IT_HARDWARE_INTERNAL",
        approved_by=ACTOR,
    )


# =================================================================== A: execution evidence is now persisted


async def test_accepted_accounting_resolution_produces_persisted_execution_evidence(session: Session) -> None:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id, invoice, facts = await _resolve_review(
        session, ettn="P0-PROD-15X-A", mapping_repository=mapping_repository
    )

    accounting_use_case = _accounting_use_case(session, facts=facts, mapping_repository=mapping_repository)
    result = await accounting_use_case.execute(_accounting_command(review_id, account_id=EXPENSE_ACCOUNT))
    session.commit()

    assert result.status.value == "resolved"
    assert result.current_review_reasons == ()
    assert result.current_workflow is WorkflowType.VENDOR_BILL

    item = session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_id))
    assert item.version == 2
    assert item.workflow == WorkflowType.VENDOR_BILL.value
    assert _reason_codes_from_payload(item.review_reasons) == set()

    evidence = _execution_evidence(session, review_id=review_id, review_version=2)
    assert evidence is not None, "execution evidence must be persisted for the new review version"
    assert evidence.operating_expense_match is not None
    assert evidence.operating_expense_match["expense_account_id"] == EXPENSE_ACCOUNT
    assert evidence.operating_expense_match["expense_category"] == "IT_HARDWARE_INTERNAL"
    assert evidence.operating_expense_match["matched_by"] == "review_accounting_resolution"


# =================================================================== B: raw classification evidence stays truthful


async def test_raw_classification_evidence_construction_is_unaffected_by_the_fix(session: Session) -> None:
    """``build_review_classification_evidence`` is called with the untouched raw
    ``decision_result`` both before and after this fix (P0-PROD-15X only threads
    the effective operating-expense match into *execution* evidence, never
    classification evidence) -- so its outcome must be identical whether or not
    an accepted ``ReviewAccountingResolution`` exists. This deterministic rule
    engine has no classification-rule component wired in, so the truthful raw
    outcome is ``None`` either way; what matters is that presence/absence of a
    review-scoped resolution never changes it.
    """

    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)

    # Baseline: no accounting resolution at all.
    baseline_invoice = _invoice(ettn="P0-PROD-15X-B-BASELINE")
    baseline_facts = _ambiguous_facts(baseline_invoice)
    baseline_review_id = await _import(
        session, invoice=baseline_invoice, facts=baseline_facts, mapping_repository=mapping_repository
    )
    baseline_classification = _classification_evidence(session, review_id=baseline_review_id, review_version=1)

    # Resolved: an accepted review-scoped accounting resolution exists.
    review_id, invoice, facts = await _resolve_review(
        session, ettn="P0-PROD-15X-B", mapping_repository=mapping_repository
    )
    accounting_use_case = _accounting_use_case(session, facts=facts, mapping_repository=mapping_repository)
    await accounting_use_case.execute(_accounting_command(review_id, account_id=EXPENSE_ACCOUNT))
    session.commit()

    item = session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_id))
    assert item.workflow == WorkflowType.VENDOR_BILL.value  # effective, unlike the raw/classification outcome

    resolved_classification = _classification_evidence(session, review_id=review_id, review_version=2)
    assert baseline_classification is None
    assert resolved_classification is None

    # The raw matcher facts fed into the deterministic engine are never mutated by
    # the effective-execution-evidence substitution (dataclasses.replace produces
    # new objects; nothing writes back into the shared raw fact objects).
    assert facts.partner_match.status is PartnerMatchStatus.MULTIPLE_MATCHES
    assert facts.partner_match.candidate_count == 2


# =================================================================== C: no supplier-wide mapping ever created


async def test_no_supplier_wide_mapping_row_is_created(session: Session) -> None:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id, invoice, facts = await _resolve_review(
        session, ettn="P0-PROD-15X-C", mapping_repository=mapping_repository
    )

    accounting_use_case = _accounting_use_case(session, facts=facts, mapping_repository=mapping_repository)
    await accounting_use_case.execute(_accounting_command(review_id, account_id=EXPENSE_ACCOUNT))
    session.commit()

    assert session.query(OperatingExpenseMappingRecord).count() == 0


# =================================================================== D: independent second review unaffected


async def test_second_review_same_supplier_without_resolution_stays_blocked(session: Session) -> None:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_a, _invoice_a, facts_a = await _resolve_review(
        session, ettn="P0-PROD-15X-D-A", mapping_repository=mapping_repository
    )
    accounting_use_case = _accounting_use_case(session, facts=facts_a, mapping_repository=mapping_repository)
    await accounting_use_case.execute(_accounting_command(review_a, account_id=EXPENSE_ACCOUNT))
    session.commit()

    invoice_b = _invoice(ettn="P0-PROD-15X-D-B")
    facts_b = _ambiguous_facts(invoice_b)
    review_b = await _import(session, invoice=invoice_b, facts=facts_b, mapping_repository=mapping_repository)

    item_b = session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_b))
    assert item_b.version == 1
    assert "OPERATING_EXPENSE_MAPPING_REQUIRED" in _reason_codes_from_payload(item_b.review_reasons)
    assert _execution_evidence(session, review_id=review_b, review_version=1) is None


# =================================================================== E: partner + operating-expense substitutions


async def test_partner_and_operating_expense_substitutions_apply_together(session: Session) -> None:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id, invoice, facts = await _resolve_review(
        session, ettn="P0-PROD-15X-E", mapping_repository=mapping_repository, create_effect=True
    )

    accounting_use_case = _accounting_use_case(session, facts=facts, mapping_repository=mapping_repository)
    await accounting_use_case.execute(_accounting_command(review_id, account_id=EXPENSE_ACCOUNT))
    session.commit()

    evidence = _execution_evidence(session, review_id=review_id, review_version=2)
    assert evidence is not None
    assert evidence.partner_match["status"] == PartnerMatchStatus.MATCHED.value
    assert evidence.partner_match["partner_id"] == CANONICAL_PARTNER_ID
    assert evidence.partner_match["matched_by"] == "supplier_remediation_effect"
    assert evidence.operating_expense_match["expense_account_id"] == EXPENSE_ACCOUNT


# =================================================================== F: supplier-wide mapping path unaffected


async def test_supplier_wide_mapping_without_review_scoped_resolution_still_resolves(session: Session) -> None:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    invoice = _invoice(ettn="P0-PROD-15X-F")
    facts = _ambiguous_facts(invoice)
    review_id = await _import(session, invoice=invoice, facts=facts, mapping_repository=mapping_repository)
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()
    mapping_repository.create(
        company_id=COMPANY_ID,
        vendor_partner_id=CANONICAL_PARTNER_ID,
        expense_account_id=SUPPLIER_WIDE_ACCOUNT,
        expense_category="SUPPLIER_WIDE_CATEGORY",
        enabled=True,
    )
    session.commit()

    reclassifier = _reclassifier(session, facts=facts, mapping_repository=mapping_repository)
    outcome = await reclassifier.execute(
        ReclassifyReviewCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=1,
            trigger=ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
        )
    )
    session.commit()

    assert "OPERATING_EXPENSE_MAPPING_REQUIRED" not in {r.code.value for r in outcome.new_review_reasons}
    evidence = _execution_evidence(session, review_id=review_id, review_version=2)
    assert evidence is not None
    assert evidence.operating_expense_match["expense_account_id"] == SUPPLIER_WIDE_ACCOUNT
    assert evidence.operating_expense_match["matched_by"] != "review_accounting_resolution"


# =================================================================== G: no resolution => unchanged behavior


async def test_no_review_scoped_resolution_leaves_execution_evidence_behavior_unchanged(session: Session) -> None:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    invoice = _invoice(ettn="P0-PROD-15X-G")
    facts = _ambiguous_facts(invoice)
    review_id = await _import(session, invoice=invoice, facts=facts, mapping_repository=mapping_repository)
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()

    reclassifier = _reclassifier(session, facts=facts, mapping_repository=mapping_repository)
    outcome = await reclassifier.execute(
        ReclassifyReviewCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=1,
            trigger=ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
        )
    )
    session.commit()

    # No supplier-wide mapping, no accounting resolution: OPERATING_EXPENSE_MAPPING_REQUIRED
    # must still be reported, and no execution evidence is persisted -- byte-for-byte the
    # same pre-P0-PROD-15X outcome for this unresolved case.
    assert "OPERATING_EXPENSE_MAPPING_REQUIRED" in {r.code.value for r in outcome.new_review_reasons}
    assert _execution_evidence(session, review_id=review_id, review_version=2) is None


# =================================================================== H: cross-review / cross-company isolation


async def test_accounting_resolution_cannot_leak_into_another_review_or_company(session: Session) -> None:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_a, _invoice_a, facts_a = await _resolve_review(
        session, ettn="P0-PROD-15X-H-A", mapping_repository=mapping_repository
    )
    accounting_use_case = _accounting_use_case(session, facts=facts_a, mapping_repository=mapping_repository)
    await accounting_use_case.execute(_accounting_command(review_a, account_id=EXPENSE_ACCOUNT))
    session.commit()

    # A second, independent review for a DIFFERENT company -- same supplier VAT,
    # same shape of invoice -- must never see review_a's resolution.
    invoice_other_company = _invoice(ettn="P0-PROD-15X-H-B")
    facts_other = _ambiguous_facts(invoice_other_company)

    use_case = ImportInvoiceUseCase(
        import_history=_FakeImportHistory(),
        decision_engine=_decision_engine(facts_other, mapping_repository=mapping_repository),
        review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(session)),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )
    result = await use_case.execute(
        ImportInvoiceCommand(
            invoice=invoice_other_company,
            idempotency_key=f"uyumsoft:{OTHER_COMPANY_ID}:{invoice_other_company.header.ettn}",
            company_id=OTHER_COMPANY_ID,
        )
    )
    review_b = result.review_id
    item_b = session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_b))
    assert item_b.company_id == OTHER_COMPANY_ID
    assert "OPERATING_EXPENSE_MAPPING_REQUIRED" in _reason_codes_from_payload(item_b.review_reasons)
    assert _execution_evidence(session, review_id=review_b, review_version=1) is None


# =================================================================== I: decision can now consume the evidence


async def test_decision_submission_no_longer_fails_with_execution_source_invoice_not_found(session: Session) -> None:
    """Direct regression for the production `execution_source_invoice_not_found` 404:
    the exact reader/query SubmitReviewDecisionUseCase relies on must now find a row.
    """

    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id, invoice, facts = await _resolve_review(
        session, ettn="P0-PROD-15X-I", mapping_repository=mapping_repository
    )

    accounting_use_case = _accounting_use_case(session, facts=facts, mapping_repository=mapping_repository)
    result = await accounting_use_case.execute(_accounting_command(review_id, account_id=EXPENSE_ACCOUNT))
    session.commit()
    assert result.current_version == 2

    evidence_reader = SqlAlchemyReviewExecutionEvidenceReader(session)
    evidence = evidence_reader.get_evidence(review_id=review_id, company_id=COMPANY_ID, expected_version=2)
    assert evidence.source_invoice_id == invoice.header.ettn
    assert evidence.operating_expense_match is not None
    assert evidence.operating_expense_match.expense_account_id == EXPENSE_ACCOUNT

    decision_use_case = SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        execution_evidence_reader=evidence_reader,
    )
    acknowledgement = decision_use_case.execute(
        ReviewDecisionCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=2,
            decision=ReviewDecisionType.SELECT_WORKFLOW,
            decided_by=ACTOR,
            selected_workflow=WorkflowType.VENDOR_BILL,
            idempotency_key="p0-prod-15x-decision-i",
        )
    )
    session.commit()
    assert acknowledgement is not None

    item = session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_id))
    assert item.version == 3
    assert item.status == "decision_submitted"
