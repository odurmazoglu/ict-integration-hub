"""P0-PROD-15P: operating-expense-mapping remediation command (use case + endpoint).

Reproduces the exact CloudSpark-shaped end-to-end gap identified in P0-PROD-15O and
proves the two-part fix:
  * ``app/application/rules/deterministic.py`` -- ``_manual_review_reasons`` no
    longer emits an operating-expense manual-review reason once the match is
    genuinely MATCHED (previously mislabeled a MATCHED result as REQUIRED whenever
    the raw partner match stayed unresolved -- unreachable via any existing test
    before this fix, since it requires partner-unmatched + expense-matched
    simultaneously).
  * ``app/application/use_cases/reclassify_review.py`` -- an accepted MATCH_EXISTING
    effect now also drives operating-expense reclassification (not just Stage-1
    evidence / SUPPLIER_AMBIGUOUS), so a review whose raw partner match stays
    ambiguous forever can still resolve OPERATING_EXPENSE_MAPPING_REQUIRED once a
    real mapping exists for the effect's resolved supplier.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_request_context, get_submit_operating_expense_mapping_use_case
from app.api.security import AuthenticationMethod, InvalidTokenError, Permission, RequestContext
from app.application.commands import ImportInvoiceCommand
from app.application.decision import (
    DecisionEngine,
    ManualReviewStrategy,
    VendorBillReviewRecommendationStrategy,
    WorkflowStrategyResolver,
)
from app.application.expense_mapping import (
    OnboardOperatingExpenseMappingUseCase,
    OperatingExpenseMappingOnboardingOutcome,
    OperatingExpenseMatchingEngine,
)
from app.application.rules.deterministic import DeterministicRuleEngine
from app.application.use_cases import ImportInvoiceUseCase
from app.application.use_cases.reclassify_review import ReclassifyWorkbenchReviewUseCase
from app.application.workbench import ReviewItemCreationService
from app.application.workbench.exceptions import (
    OperatingExpenseMappingAccountInvalidError,
    OperatingExpenseMappingEligibilityError,
    OperatingExpenseMappingSupplierUnresolvedError,
    ReviewNotFoundError,
    ReviewVersionConflictError,
)
from app.application.workbench.expense_account_lookup import ExpenseAccountCandidate
from app.application.workbench.operating_expense_mapping_command import (
    OperatingExpenseMappingSubmissionResult,
    OperatingExpenseMappingSubmissionStatus,
    SubmitOperatingExpenseMappingCommand,
)
from app.application.workbench.operating_expense_mapping_use_cases import SubmitOperatingExpenseMappingUseCase
from app.application.workbench.supplier_remediation import SupplierPartnerWriteEffectStatus, SupplierRemediationEffect
from app.application.workbench.supplier_resolution import SupplierResolutionMode
from app.application.workflow import ManualReviewReasonCode, WorkflowType
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.main import app
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.models.operating_expense_mapping import OperatingExpenseMappingRecord
from app.models.workbench_review_classification_evidence import WorkbenchReviewClassificationEvidence
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_reclassification import WorkbenchReviewReclassification
from app.models.workbench_review_source_invoice_evidence import WorkbenchReviewSourceInvoiceEvidence
from app.models.workbench_review_supplier_remediation_effect import WorkbenchReviewSupplierRemediationEffect
from app.persistence import (
    SqlAlchemyOperatingExpenseMappingRepository,
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
EXPENSE_ACCOUNT_ID = 5501
TAX_ID = 8801
ETTN = "P0-PROD-15P-ETTN"
IDEMPOTENCY_KEY = f"uyumsoft:{COMPANY_ID}:{ETTN}"
ACTOR = "p0-prod-15p-test-operator"


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
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


# --------------------------------------------------------------------------- builders


def _invoice() -> InternalInvoice:
    """Shaped like the real CloudSpark production invoice: identifier-free lines,
    20% KDV, no operating-expense mapping configured yet.
    """

    return InternalInvoice(
        header=Header(
            invoice_number="I082026000000009",
            invoice_uuid=ETTN,
            ettn=ETTN,
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


def _partner_ambiguous(*, candidate_count: int = 2) -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MULTIPLE_MATCHES,
        partner_id=None,
        matched_by=None,
        reason="Multiple active supplier partner candidates found by tax number.",
        candidate_count=candidate_count,
        confidence=None,
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
                    reason="No active deterministic product candidate found.",
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


async def _import(session: Session, *, invoice: InternalInvoice, mapping_repository) -> str:
    use_case = ImportInvoiceUseCase(
        import_history=_FakeImportHistory(),
        decision_engine=_decision_engine(_ambiguous_facts(invoice), mapping_repository=mapping_repository),
        review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(session)),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )
    result = await use_case.execute(
        ImportInvoiceCommand(invoice=invoice, idempotency_key=IDEMPOTENCY_KEY, company_id=COMPANY_ID)
    )
    assert result.review_id is not None
    return result.review_id


def _effect(*, review_id: str, company_id: int = COMPANY_ID, partner_id: int = CANONICAL_PARTNER_ID):
    return SupplierRemediationEffect(
        review_id=review_id,
        company_id=company_id,
        review_version=1,
        source_invoice_id=ETTN,
        mode=SupplierResolutionMode.MATCH_EXISTING,
        resolved_partner_id=partner_id,
        partner_write_status=SupplierPartnerWriteEffectStatus.SELECTED,
    )


def _canonical_account(*, id_: int = EXPENSE_ACCOUNT_ID) -> ExpenseAccountCandidate:
    return ExpenseAccountCandidate(id=id_, code="770.01", name="General Expenses", account_type="expense")


class _FakeExpenseAccountReader:
    def __init__(self, *, accounts: tuple[ExpenseAccountCandidate, ...] = ()) -> None:
        self._by_company: dict[int, dict[int, ExpenseAccountCandidate]] = {COMPANY_ID: {a.id: a for a in accounts}}
        self.calls: list[tuple[int, int]] = []

    def find_candidates(self, *, company_id: int, query: str | None) -> tuple[ExpenseAccountCandidate, ...]:
        return tuple(self._by_company.get(company_id, {}).values())

    def find_eligible_by_id(self, *, company_id: int, account_id: int) -> ExpenseAccountCandidate | None:
        self.calls.append((company_id, account_id))
        return self._by_company.get(company_id, {}).get(account_id)


def _use_case(
    session: Session,
    *,
    invoice: InternalInvoice,
    accounts: tuple[ExpenseAccountCandidate, ...] | None = None,
) -> SubmitOperatingExpenseMappingUseCase:
    review_repository = SqlAlchemyReviewRepository(session)
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    resolved_accounts = (_canonical_account(),) if accounts is None else accounts
    return SubmitOperatingExpenseMappingUseCase(
        review_reader=review_repository,
        remediation_effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        expense_account_reader=_FakeExpenseAccountReader(accounts=resolved_accounts),
        mapping_repository=mapping_repository,
        onboarding_use_case=OnboardOperatingExpenseMappingUseCase(mapping_repository),
        reclassifier=ReclassifyWorkbenchReviewUseCase(
            decision_engine=_decision_engine(_ambiguous_facts(invoice), mapping_repository=mapping_repository),
            source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
            reclassification_writer=review_repository,
            supplier_remediation_effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
            operating_expense_matcher=OperatingExpenseMatchingEngine(mapping_repository),
        ),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


def _command(
    review_id: str, *, expected_version: int = 1, account_id: int = EXPENSE_ACCOUNT_ID, category: str = "OPEX"
) -> SubmitOperatingExpenseMappingCommand:
    return SubmitOperatingExpenseMappingCommand(
        review_id=review_id,
        company_id=COMPANY_ID,
        expected_version=expected_version,
        expense_account_id=account_id,
        expense_category=category,
        approved_by=ACTOR,
    )


def _reason_codes(codes) -> set[str]:
    return {c.value if hasattr(c, "value") else c for c in codes}


# =================================================================== rule-engine bugfix (unrelated reason preserved)


def test_partner_ambiguous_and_expense_matched_yields_no_operating_expense_reason() -> None:
    """Direct regression test for the app/application/rules/deterministic.py fix:
    an unrelated reason (SUPPLIER_AMBIGUOUS) must survive, while a genuinely MATCHED
    operating-expense result must never be mislabeled as REQUIRED.
    """

    from app.application.expense_mapping.matching import (
        EXACT_MATCH_CONFIDENCE,
        MATCHED_BY_COMPANY_PARTNER,
        OperatingExpenseMatchResult,
        OperatingExpenseMatchStatus,
    )

    invoice = _invoice()
    matched_expense = OperatingExpenseMatchResult(
        status=OperatingExpenseMatchStatus.MATCHED,
        reason="matched",
        candidate_count=1,
        mapping_id=1,
        company_id=COMPANY_ID,
        vendor_partner_id=CANONICAL_PARTNER_ID,
        expense_account_id=EXPENSE_ACCOUNT_ID,
        expense_category="OPEX",
        matched_by=MATCHED_BY_COMPANY_PARTNER,
        confidence=EXACT_MATCH_CONFIDENCE,
    )

    class _FixedOperatingExpenseMatcher:
        def match_invoice(self, invoice: InternalInvoice, *, company_id: int, partner_match: PartnerMatchResult):
            return matched_expense

    engine = DeterministicRuleEngine(
        partner_matcher=_MatchingFacts(_partner_ambiguous()),
        product_matcher=_MatchingFacts(_product_not_found(invoice)),
        tax_mapper=_MatchingFacts(_taxes(invoice)),
        operating_expense_matcher=_FixedOperatingExpenseMatcher(),
    )
    result = engine.evaluate(ImportInvoiceCommand(invoice=invoice, idempotency_key="x", company_id=COMPANY_ID))

    assert result.workflow.value == "manual_review"
    codes = {r.code for r in result.workflow_decision.manual_review.reasons}
    assert ManualReviewReasonCode.SUPPLIER_AMBIGUOUS in codes
    assert ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED not in codes
    assert ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_AMBIGUOUS not in codes


# =================================================================== end-to-end CloudSpark-shaped resolution (18, 19)


async def test_full_cloudspark_shaped_mapping_resolves_operating_expense_requirement(session: Session) -> None:
    invoice = _invoice()
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(session, invoice=invoice, mapping_repository=mapping_repository)

    item = session.scalar(select(WorkbenchReviewItem))
    assert _reason_codes(c["code"] for c in item.review_reasons) == {
        "SUPPLIER_AMBIGUOUS",
        "OPERATING_EXPENSE_MAPPING_REQUIRED",
    }

    # Supplier already resolved via MATCH_EXISTING (mirrors the real P0-PROD-15N/15O
    # resume that already ran before this endpoint is ever reached).
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id))
    session.commit()

    use_case = _use_case(session, invoice=invoice)
    result = await use_case.execute(_command(review_id))
    session.commit()

    assert result.status is OperatingExpenseMappingSubmissionStatus.RESOLVED
    assert result.reclassified is True
    assert result.vendor_partner_id == CANONICAL_PARTNER_ID
    codes = _reason_codes(r.code for r in result.current_review_reasons)
    assert "OPERATING_EXPENSE_MAPPING_REQUIRED" not in codes
    # SUPPLIER_AMBIGUOUS also stays cleared (the raw matcher itself is still
    # ambiguous forever, but the accepted effect keeps resolving it every reclassify).
    assert "SUPPLIER_AMBIGUOUS" not in codes

    item = session.scalar(select(WorkbenchReviewItem))
    assert _reason_codes(c["code"] for c in item.review_reasons) == set()
    assert item.workflow == WorkflowType.VENDOR_BILL.value

    mapping_row = session.scalar(select(OperatingExpenseMappingRecord))
    assert mapping_row.vendor_partner_id == CANONICAL_PARTNER_ID
    assert mapping_row.expense_account_id == EXPENSE_ACCOUNT_ID


# =================================================================== 10: not found / wrong company


async def test_review_not_found_fails_safely(session: Session) -> None:
    invoice = _invoice()
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    await _import(session, invoice=invoice, mapping_repository=mapping_repository)
    use_case = _use_case(session, invoice=invoice)

    with pytest.raises(ReviewNotFoundError):
        await use_case.execute(_command("review:does-not-exist"))


async def test_wrong_company_fails_safely(session: Session) -> None:
    invoice = _invoice()
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(session, invoice=invoice, mapping_repository=mapping_repository)
    use_case = SubmitOperatingExpenseMappingUseCase(
        review_reader=SqlAlchemyReviewRepository(session),
        remediation_effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        expense_account_reader=_FakeExpenseAccountReader(accounts=(_canonical_account(),)),
        mapping_repository=mapping_repository,
        onboarding_use_case=OnboardOperatingExpenseMappingUseCase(mapping_repository),
        reclassifier=ReclassifyWorkbenchReviewUseCase(
            decision_engine=_decision_engine(_ambiguous_facts(invoice), mapping_repository=mapping_repository),
            source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
            reclassification_writer=SqlAlchemyReviewRepository(session),
        ),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )
    command = SubmitOperatingExpenseMappingCommand(
        review_id=review_id,
        company_id=OTHER_COMPANY_ID,
        expected_version=1,
        expense_account_id=EXPENSE_ACCOUNT_ID,
        expense_category="OPEX",
        approved_by=ACTOR,
    )
    with pytest.raises(ReviewNotFoundError):
        await use_case.execute(command)
    assert session.query(OperatingExpenseMappingRecord).count() == 0


# =================================================================== 11: eligibility


async def test_review_without_operating_expense_required_cannot_be_remapped(session: Session) -> None:
    invoice = _invoice()
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(session, invoice=invoice, mapping_repository=mapping_repository)
    # Manually clear the review's reasons to simulate "already resolved by something else".
    item = session.scalar(select(WorkbenchReviewItem))
    item.review_reasons = []
    session.commit()

    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id))
    session.commit()

    use_case = _use_case(session, invoice=invoice)
    with pytest.raises(OperatingExpenseMappingEligibilityError):
        await use_case.execute(_command(review_id))


# =================================================================== 12: optimistic concurrency


async def test_stale_expected_version_conflicts(session: Session) -> None:
    invoice = _invoice()
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(session, invoice=invoice, mapping_repository=mapping_repository)
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id))
    session.commit()

    use_case = _use_case(session, invoice=invoice)
    with pytest.raises(ReviewVersionConflictError):
        await use_case.execute(_command(review_id, expected_version=99))
    assert session.query(OperatingExpenseMappingRecord).count() == 0


# =================================================================== 13: invalid account rejected


async def test_invalid_account_is_rejected(session: Session) -> None:
    invoice = _invoice()
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(session, invoice=invoice, mapping_repository=mapping_repository)
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id))
    session.commit()

    use_case = _use_case(session, invoice=invoice, accounts=())  # no eligible accounts at all
    with pytest.raises(OperatingExpenseMappingAccountInvalidError):
        await use_case.execute(_command(review_id))
    assert session.query(OperatingExpenseMappingRecord).count() == 0


# =================================================================== 14: cross-company account rejected


async def test_account_scoped_to_another_company_is_rejected(session: Session) -> None:
    invoice = _invoice()
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(session, invoice=invoice, mapping_repository=mapping_repository)
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id))
    session.commit()

    reader = _FakeExpenseAccountReader(accounts=())  # simulates: exists elsewhere, not for COMPANY_ID
    use_case = SubmitOperatingExpenseMappingUseCase(
        review_reader=SqlAlchemyReviewRepository(session),
        remediation_effect_reader=effect_repo,
        expense_account_reader=reader,
        mapping_repository=mapping_repository,
        onboarding_use_case=OnboardOperatingExpenseMappingUseCase(mapping_repository),
        reclassifier=ReclassifyWorkbenchReviewUseCase(
            decision_engine=_decision_engine(_ambiguous_facts(invoice), mapping_repository=mapping_repository),
            source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
            reclassification_writer=SqlAlchemyReviewRepository(session),
            supplier_remediation_effect_reader=effect_repo,
        ),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )
    with pytest.raises(OperatingExpenseMappingAccountInvalidError):
        await use_case.execute(_command(review_id))
    assert reader.calls == [(COMPANY_ID, EXPENSE_ACCOUNT_ID)]


# =================================================================== supplier unresolved


async def test_no_supplier_effect_is_rejected(session: Session) -> None:
    invoice = _invoice()
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(session, invoice=invoice, mapping_repository=mapping_repository)
    use_case = _use_case(session, invoice=invoice)

    with pytest.raises(OperatingExpenseMappingSupplierUnresolvedError):
        await use_case.execute(_command(review_id))


# =================================================================== 15, 16: onboarding reuse, persists once


async def test_successful_mapping_persists_exactly_once_via_existing_onboarding_use_case(session: Session) -> None:
    invoice = _invoice()
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(session, invoice=invoice, mapping_repository=mapping_repository)
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id))
    session.commit()

    use_case = _use_case(session, invoice=invoice)
    result = await use_case.execute(_command(review_id))
    session.commit()

    assert result.mapping_outcome is OperatingExpenseMappingOnboardingOutcome.CREATED
    rows = session.query(OperatingExpenseMappingRecord).all()
    assert len(rows) == 1
    assert rows[0].vendor_partner_id == CANONICAL_PARTNER_ID
    assert rows[0].expense_account_id == EXPENSE_ACCOUNT_ID
    assert rows[0].expense_category == "OPEX"


# =================================================================== 17: retry/resume, no duplicate


async def test_resume_after_full_success_does_not_duplicate_mapping_or_reclassify_again(session: Session) -> None:
    invoice = _invoice()
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(session, invoice=invoice, mapping_repository=mapping_repository)
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id))
    session.commit()

    use_case = _use_case(session, invoice=invoice)
    first = await use_case.execute(_command(review_id))
    session.commit()
    assert first.already_applied is False
    assert first.current_version == 2

    second = await use_case.execute(_command(review_id))
    session.commit()

    assert second.already_applied is True
    assert second.current_version == 2
    assert session.query(OperatingExpenseMappingRecord).count() == 1


async def test_resume_with_different_account_still_conflicts(session: Session) -> None:
    invoice = _invoice()
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(session, invoice=invoice, mapping_repository=mapping_repository)
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id))
    session.commit()

    use_case = _use_case(session, invoice=invoice, accounts=(_canonical_account(), _canonical_account(id_=6001)))
    await use_case.execute(_command(review_id))
    session.commit()

    with pytest.raises(ReviewVersionConflictError):
        await use_case.execute(_command(review_id, account_id=6001))


# =================================================================== response truthfulness (via router)


def _context(*permissions: Permission, company_id: int = COMPANY_ID) -> RequestContext:
    return RequestContext(
        user_id="op-1",
        user_name="Ops One",
        company_id=company_id,
        permissions=permissions,
        trace_id="trace-oem",
        authentication_method=AuthenticationMethod.JWT,
    )


class _FakeSubmitUseCase:
    def __init__(
        self,
        *,
        result: OperatingExpenseMappingSubmissionResult | None = None,
        error: Exception | None = None,
    ):
        self._result = result
        self._error = error
        self.commands: list[SubmitOperatingExpenseMappingCommand] = []

    async def execute(self, command: SubmitOperatingExpenseMappingCommand) -> OperatingExpenseMappingSubmissionResult:
        self.commands.append(command)
        if self._error is not None:
            raise self._error
        return self._result  # type: ignore[return-value]


def _fake_result(**kw: Any) -> OperatingExpenseMappingSubmissionResult:
    base: dict[str, Any] = {
        "review_id": "review:endpoint-oem-1",
        "company_id": COMPANY_ID,
        "status": OperatingExpenseMappingSubmissionStatus.RESOLVED,
        "previous_version": 1,
        "current_version": 2,
        "current_workflow": WorkflowType.VENDOR_BILL,
        "current_review_reasons": (),
        "vendor_partner_id": CANONICAL_PARTNER_ID,
        "expense_account_id": EXPENSE_ACCOUNT_ID,
        "expense_category": "OPEX",
        "mapping_outcome": OperatingExpenseMappingOnboardingOutcome.CREATED,
        "reclassified": True,
        "already_applied": False,
        "safe_message": "Operating-expense mapping configured; the review was reclassified.",
    }
    base.update(kw)
    return OperatingExpenseMappingSubmissionResult(**base)


async def _post(api_client: AsyncClient, *, context: RequestContext, json: dict[str, Any], use_case=None):
    app.dependency_overrides[get_request_context] = lambda: context
    if use_case is not None:
        app.dependency_overrides[get_submit_operating_expense_mapping_use_case] = lambda: use_case
    try:
        return await api_client.post(
            "/api/workbench/reviews/review:endpoint-oem-1/operating-expense-mapping", json=json
        )
    finally:
        app.dependency_overrides.clear()


async def test_endpoint_happy_path(api_client: AsyncClient) -> None:
    use_case = _FakeSubmitUseCase(result=_fake_result())
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={"expected_version": 1, "expense_account_id": EXPENSE_ACCOUNT_ID, "expense_category": "OPEX"},
        use_case=use_case,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["resolution_status"] == "resolved"
    assert data["vendor_partner_id"] == CANONICAL_PARTNER_ID
    assert data["expense_account_id"] == EXPENSE_ACCOUNT_ID
    command = use_case.commands[0]
    assert command.company_id == COMPANY_ID  # from context, never the body
    assert command.approved_by == "Ops One"


async def test_endpoint_body_cannot_set_identity_fields(api_client: AsyncClient) -> None:
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={
            "expected_version": 1,
            "expense_account_id": EXPENSE_ACCOUNT_ID,
            "expense_category": "OPEX",
            "company_id": 999,
            "vendor_partner_id": 1,
        },
        use_case=_FakeSubmitUseCase(result=_fake_result()),
    )
    assert response.status_code == 400


async def test_endpoint_missing_permission_maps_to_403(api_client: AsyncClient) -> None:
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_READ),
        json={"expected_version": 1, "expense_account_id": EXPENSE_ACCOUNT_ID, "expense_category": "OPEX"},
        use_case=_FakeSubmitUseCase(result=_fake_result()),
    )
    assert response.status_code == 403


async def test_endpoint_authentication_failure_maps_to_401(api_client: AsyncClient) -> None:
    app.dependency_overrides[get_request_context] = lambda: (_ for _ in ()).throw(
        InvalidTokenError("Bearer token is invalid.")
    )
    try:
        response = await api_client.post(
            "/api/workbench/reviews/review:endpoint-oem-1/operating-expense-mapping",
            json={"expected_version": 1, "expense_account_id": EXPENSE_ACCOUNT_ID, "expense_category": "OPEX"},
            headers={"Authorization": "Bearer secret-token", "X-Trace-ID": "trace-401-oem"},
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 401
    assert response.json()["trace_id"] == "trace-401-oem"
    assert "secret-token" not in response.text


async def test_endpoint_version_conflict_maps_to_409(api_client: AsyncClient) -> None:
    use_case = _FakeSubmitUseCase(error=ReviewVersionConflictError("stale"))
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={"expected_version": 1, "expense_account_id": EXPENSE_ACCOUNT_ID, "expense_category": "OPEX"},
        use_case=use_case,
    )
    assert response.status_code == 409


async def test_endpoint_invalid_account_maps_to_400(api_client: AsyncClient) -> None:
    use_case = _FakeSubmitUseCase(error=OperatingExpenseMappingAccountInvalidError("bad account"))
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={"expected_version": 1, "expense_account_id": EXPENSE_ACCOUNT_ID, "expense_category": "OPEX"},
        use_case=use_case,
    )
    assert response.status_code == 400


async def test_endpoint_supplier_unresolved_maps_to_409(api_client: AsyncClient) -> None:
    use_case = _FakeSubmitUseCase(error=OperatingExpenseMappingSupplierUnresolvedError("resolve supplier first"))
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={"expected_version": 1, "expense_account_id": EXPENSE_ACCOUNT_ID, "expense_category": "OPEX"},
        use_case=use_case,
    )
    assert response.status_code == 409
