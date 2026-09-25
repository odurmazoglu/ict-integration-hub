"""P0-PROD-15T: review-scoped purchase-purpose + accounting-resolution.

Proves the two new immutable, review-scoped effects (never the supplier-wide
``operating_expense_mappings`` table) and the reclassification precedence:

    review-scoped ReviewAccountingResolution  >  supplier-wide mapping  >  manual review

The key acceptance scenario mirrors the real production gap: CloudSpark
(partner 439) is a mixed-purpose supplier. Review A records purchase_purpose
INTERNAL_USE and an accounting resolution, and resolves. Review B, an
independent identifier-free invoice from the SAME supplier with no
review-scoped resolution, must remain blocked -- and no supplier-wide
operating_expense_mapping for partner 439 must ever exist, proving today's
internal-use decision cannot contaminate tomorrow's resale/customer-project
invoice.
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

from app.api.dependencies import (
    get_request_context,
    get_submit_purchase_purpose_use_case,
    get_submit_review_accounting_resolution_use_case,
)
from app.api.security import AuthenticationMethod, InvalidTokenError, Permission, RequestContext
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
    AccountingResolutionStatus,
    AccountingTreatmentType,
    ReviewAccountingResolutionSubmissionResult,
    SubmitReviewAccountingResolutionCommand,
)
from app.application.workbench.accounting_resolution_use_cases import SubmitReviewAccountingResolutionUseCase
from app.application.workbench.exceptions import (
    AccountingResolutionConflictError,
    AccountingResolutionPurposeRequiredError,
    AccountingResolutionPurposeUnsupportedError,
    OperatingExpenseMappingAccountInvalidError,
    PurchasePurposeConflictError,
    ReviewNotFoundError,
    ReviewVersionConflictError,
)
from app.application.workbench.expense_account_lookup import ExpenseAccountCandidate
from app.application.workbench.purchase_purpose import (
    PurchasePurpose,
    PurchasePurposeResolution,
    PurchasePurposeSubmissionResult,
    SubmitPurchasePurposeCommand,
)
from app.application.workbench.purchase_purpose_use_cases import SubmitPurchasePurposeUseCase
from app.application.workbench.supplier_remediation import SupplierPartnerWriteEffectStatus, SupplierRemediationEffect
from app.application.workbench.supplier_resolution import SupplierResolutionMode
from app.application.workflow import WorkflowType
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
from app.models.workbench_review_accounting_resolution import WorkbenchReviewAccountingResolution
from app.models.workbench_review_classification_evidence import WorkbenchReviewClassificationEvidence
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
EXPENSE_ACCOUNT_A = 193
EXPENSE_ACCOUNT_B = 247
TAX_ID = 8801
ACTOR = "p0-prod-15t-test-operator"


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
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


# --------------------------------------------------------------------------- builders


def _invoice(*, ettn: str) -> InternalInvoice:
    """Shaped like the real CloudSpark production invoice: identifier-free lines,
    20% KDV, no supplier-wide operating-expense mapping.
    """

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


def _partner_not_found() -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.NOT_FOUND,
        partner_id=None,
        matched_by=None,
        reason="No active deterministic supplier partner candidate found.",
        candidate_count=0,
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


def _not_found_facts(invoice: InternalInvoice) -> _Facts:
    return _Facts(_partner_not_found(), _product_not_found(invoice), _taxes(invoice))


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


def _canonical_account(*, id_: int = EXPENSE_ACCOUNT_A) -> ExpenseAccountCandidate:
    return ExpenseAccountCandidate(
        id=id_, code="632000", name="General And Administrative Expenses", account_type="expense"
    )


class _FakeExpenseAccountReader:
    def __init__(self, *, accounts: tuple[ExpenseAccountCandidate, ...] | None = None) -> None:
        resolved = (_canonical_account(), _canonical_account(id_=EXPENSE_ACCOUNT_B)) if accounts is None else accounts
        self._by_company: dict[int, dict[int, ExpenseAccountCandidate]] = {COMPANY_ID: {a.id: a for a in resolved}}
        self.calls: list[tuple[int, int]] = []

    def find_candidates(self, *, company_id: int, query: str | None) -> tuple[ExpenseAccountCandidate, ...]:
        return tuple(self._by_company.get(company_id, {}).values())

    def find_eligible_by_id(self, *, company_id: int, account_id: int) -> ExpenseAccountCandidate | None:
        self.calls.append((company_id, account_id))
        return self._by_company.get(company_id, {}).get(account_id)


def _purpose_use_case(session: Session) -> SubmitPurchasePurposeUseCase:
    return SubmitPurchasePurposeUseCase(
        review_reader=SqlAlchemyReviewRepository(session),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        purpose_writer=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


def _accounting_use_case(
    session: Session,
    *,
    facts: _Facts,
    accounts: tuple[ExpenseAccountCandidate, ...] | None = None,
) -> SubmitReviewAccountingResolutionUseCase:

    review_repository = SqlAlchemyReviewRepository(session)
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    accounting_resolution_repository = SqlAlchemyReviewAccountingResolutionRepository(session)
    return SubmitReviewAccountingResolutionUseCase(
        review_reader=review_repository,
        purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        expense_account_reader=_FakeExpenseAccountReader(accounts=accounts),
        accounting_resolution_writer=accounting_resolution_repository,
        reclassifier=ReclassifyWorkbenchReviewUseCase(
            decision_engine=_decision_engine(facts, mapping_repository=mapping_repository),
            source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
            reclassification_writer=review_repository,
            supplier_remediation_effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
            operating_expense_matcher=OperatingExpenseMatchingEngine(mapping_repository),
            review_accounting_resolution_reader=accounting_resolution_repository,
        ),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


def _purpose_command(
    review_id: str, *, expected_version: int = 1, purpose: PurchasePurpose = PurchasePurpose.INTERNAL_USE
) -> SubmitPurchasePurposeCommand:
    return SubmitPurchasePurposeCommand(
        review_id=review_id,
        company_id=COMPANY_ID,
        expected_version=expected_version,
        purchase_purpose=purpose,
        approved_by=ACTOR,
    )


def _accounting_command(
    review_id: str,
    *,
    expected_version: int = 1,
    account_id: int = EXPENSE_ACCOUNT_A,
    category: str = "IT_HARDWARE_INTERNAL",
) -> SubmitReviewAccountingResolutionCommand:
    return SubmitReviewAccountingResolutionCommand(
        review_id=review_id,
        company_id=COMPANY_ID,
        expected_version=expected_version,
        treatment_type=AccountingTreatmentType.EXPENSE_ACCOUNT,
        expense_account_id=account_id,
        expense_category=category,
        approved_by=ACTOR,
    )


def _reason_codes(reasons) -> set[str]:
    return {r.code.value for r in reasons}


# =================================================================== 1, 6: purpose persistence + source id


async def test_purchase_purpose_persists_and_derives_source_invoice_id_server_side(session: Session) -> None:
    invoice = _invoice(ettn="P0-PROD-15T-A")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )

    use_case = _purpose_use_case(session)
    result = use_case.execute(_purpose_command(review_id))
    session.commit()

    assert isinstance(result, PurchasePurposeSubmissionResult)
    assert result.purchase_purpose is PurchasePurpose.INTERNAL_USE
    assert result.already_applied is False

    row = session.scalar(select(WorkbenchReviewPurchasePurposeResolution))
    assert row.source_invoice_id == invoice.header.ettn
    assert row.purchase_purpose == "internal_use"

    # Recording purpose must not reclassify or advance the review version.
    item = session.scalar(select(WorkbenchReviewItem))
    assert item.version == 1


# =================================================================== 2: identical retry


async def test_purchase_purpose_identical_retry_reports_already_applied(session: Session) -> None:
    invoice = _invoice(ettn="P0-PROD-15T-B")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )
    use_case = _purpose_use_case(session)

    first = use_case.execute(_purpose_command(review_id))
    session.commit()
    second = use_case.execute(_purpose_command(review_id))
    session.commit()

    assert first.already_applied is False
    assert second.already_applied is True
    assert session.query(WorkbenchReviewPurchasePurposeResolution).count() == 1


# =================================================================== 3: conflicting retry


async def test_purchase_purpose_conflicting_retry_raises_conflict(session: Session) -> None:
    invoice = _invoice(ettn="P0-PROD-15T-C")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )
    use_case = _purpose_use_case(session)
    use_case.execute(_purpose_command(review_id, purpose=PurchasePurpose.INTERNAL_USE))
    session.commit()

    with pytest.raises(PurchasePurposeConflictError):
        use_case.execute(_purpose_command(review_id, purpose=PurchasePurpose.RESALE))
    assert session.query(WorkbenchReviewPurchasePurposeResolution).count() == 1


# =================================================================== 4: company isolation (purpose)


async def test_purchase_purpose_wrong_company_fails_safely(session: Session) -> None:
    invoice = _invoice(ettn="P0-PROD-15T-D")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )
    use_case = _purpose_use_case(session)

    bad_command = SubmitPurchasePurposeCommand(
        review_id=review_id,
        company_id=OTHER_COMPANY_ID,
        expected_version=1,
        purchase_purpose=PurchasePurpose.INTERNAL_USE,
        approved_by=ACTOR,
    )
    with pytest.raises(ReviewNotFoundError):
        use_case.execute(bad_command)
    assert session.query(WorkbenchReviewPurchasePurposeResolution).count() == 0


# =================================================================== 5: optimistic concurrency


async def test_purchase_purpose_stale_version_conflicts(session: Session) -> None:
    invoice = _invoice(ettn="P0-PROD-15T-E")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )
    use_case = _purpose_use_case(session)

    with pytest.raises(ReviewVersionConflictError):
        use_case.execute(_purpose_command(review_id, expected_version=99))
    assert session.query(WorkbenchReviewPurchasePurposeResolution).count() == 0


async def test_accounting_resolution_stale_version_conflicts(session: Session) -> None:
    invoice = _invoice(ettn="P0-PROD-15T-F")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()
    _purpose_use_case(session).execute(_purpose_command(review_id))
    session.commit()

    accounting_use_case = _accounting_use_case(session, facts=_ambiguous_facts(invoice))
    with pytest.raises(ReviewVersionConflictError):
        await accounting_use_case.execute(_accounting_command(review_id, expected_version=99))
    assert session.query(WorkbenchReviewAccountingResolution).count() == 0


# =================================================================== 7: accounting resolution requires purpose


async def test_accounting_resolution_requires_purpose_first(session: Session) -> None:
    invoice = _invoice(ettn="P0-PROD-15T-G")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )
    accounting_use_case = _accounting_use_case(session, facts=_ambiguous_facts(invoice))

    with pytest.raises(AccountingResolutionPurposeRequiredError):
        await accounting_use_case.execute(_accounting_command(review_id))
    assert session.query(WorkbenchReviewAccountingResolution).count() == 0


# =================================================================== 8, 9: supported purposes


async def test_internal_use_purpose_supports_expense_account(session: Session) -> None:
    invoice = _invoice(ettn="P0-PROD-15T-H")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()
    _purpose_use_case(session).execute(_purpose_command(review_id, purpose=PurchasePurpose.INTERNAL_USE))
    session.commit()

    accounting_use_case = _accounting_use_case(session, facts=_ambiguous_facts(invoice))
    result = await accounting_use_case.execute(_accounting_command(review_id))
    session.commit()

    assert result.status is AccountingResolutionStatus.RESOLVED
    assert "OPERATING_EXPENSE_MAPPING_REQUIRED" not in _reason_codes(result.current_review_reasons)


async def test_other_operating_expense_purpose_supports_expense_account(session: Session) -> None:
    invoice = _invoice(ettn="P0-PROD-15T-I")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()
    _purpose_use_case(session).execute(_purpose_command(review_id, purpose=PurchasePurpose.OTHER_OPERATING_EXPENSE))
    session.commit()

    accounting_use_case = _accounting_use_case(session, facts=_ambiguous_facts(invoice))
    result = await accounting_use_case.execute(_accounting_command(review_id))
    session.commit()

    assert result.status is AccountingResolutionStatus.RESOLVED


# =================================================================== 10, 11: unsupported purposes rejected


# P0-PROD-18E-1B: RESALE can no longer be *submitted* on this identifier-free review, so
# its case records the purpose directly -- a row that may predate 18E-1B -- and proves
# accounting resolution still rejects it.
@pytest.mark.parametrize("purpose", [PurchasePurpose.RESALE, PurchasePurpose.CUSTOMER_PROJECT])
async def test_unsupported_purpose_rejects_expense_account(session: Session, purpose: PurchasePurpose) -> None:
    invoice = _invoice(ettn=f"P0-PROD-15T-J-{purpose.value}")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()
    if purpose is PurchasePurpose.RESALE:
        SqlAlchemyReviewPurchasePurposeResolutionRepository(session).create_purchase_purpose_resolution(
            PurchasePurposeResolution(
                review_id=review_id,
                company_id=COMPANY_ID,
                review_version=1,
                source_invoice_id=invoice.header.ettn,
                purchase_purpose=purpose,
                approved_by="operator",
            )
        )
    else:
        _purpose_use_case(session).execute(_purpose_command(review_id, purpose=purpose))
    session.commit()

    accounting_use_case = _accounting_use_case(session, facts=_ambiguous_facts(invoice))
    with pytest.raises(AccountingResolutionPurposeUnsupportedError):
        await accounting_use_case.execute(_accounting_command(review_id))
    assert session.query(WorkbenchReviewAccountingResolution).count() == 0
    # The review must remain manual_review, unreclassified -- never silently
    # reinterpreted as a plain expense.
    item = session.scalar(select(WorkbenchReviewItem))
    assert item.version == 1
    assert item.status == "pending_review"
    assert "OPERATING_EXPENSE_MAPPING_REQUIRED" in _reason_codes_from_payload(item.review_reasons)


# =================================================================== 12, 13: account validation


async def test_invalid_account_is_rejected(session: Session) -> None:
    invoice = _invoice(ettn="P0-PROD-15T-K")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()
    _purpose_use_case(session).execute(_purpose_command(review_id))
    session.commit()

    accounting_use_case = _accounting_use_case(session, facts=_ambiguous_facts(invoice), accounts=())
    with pytest.raises(OperatingExpenseMappingAccountInvalidError):
        await accounting_use_case.execute(_accounting_command(review_id))
    assert session.query(WorkbenchReviewAccountingResolution).count() == 0


async def test_cross_company_account_is_rejected(session: Session) -> None:
    invoice = _invoice(ettn="P0-PROD-15T-L")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()
    _purpose_use_case(session).execute(_purpose_command(review_id))
    session.commit()

    reader = _FakeExpenseAccountReader(accounts=())  # simulates: account exists, but not for COMPANY_ID

    review_repository = SqlAlchemyReviewRepository(session)
    accounting_resolution_repository = SqlAlchemyReviewAccountingResolutionRepository(session)
    accounting_use_case = SubmitReviewAccountingResolutionUseCase(
        review_reader=review_repository,
        purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        expense_account_reader=reader,
        accounting_resolution_writer=accounting_resolution_repository,
        reclassifier=ReclassifyWorkbenchReviewUseCase(
            decision_engine=_decision_engine(_ambiguous_facts(invoice), mapping_repository=mapping_repository),
            source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
            reclassification_writer=review_repository,
            supplier_remediation_effect_reader=effect_repo,
        ),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )
    with pytest.raises(OperatingExpenseMappingAccountInvalidError):
        await accounting_use_case.execute(_accounting_command(review_id))
    assert reader.calls == [(COMPANY_ID, EXPENSE_ACCOUNT_A)]


# =================================================================== 14, 15, 17: accounting resolution retry


async def test_accounting_resolution_identical_retry_reports_already_applied(session: Session) -> None:
    invoice = _invoice(ettn="P0-PROD-15T-M")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()
    _purpose_use_case(session).execute(_purpose_command(review_id))
    session.commit()

    accounting_use_case = _accounting_use_case(session, facts=_ambiguous_facts(invoice))
    first = await accounting_use_case.execute(_accounting_command(review_id))
    session.commit()
    assert first.already_applied is False
    assert first.current_version == 2

    second = await accounting_use_case.execute(_accounting_command(review_id))
    session.commit()
    assert second.already_applied is True
    assert second.current_version == 2
    assert session.query(WorkbenchReviewAccountingResolution).count() == 1


async def test_accounting_resolution_conflicting_retry_raises_conflict(session: Session) -> None:
    """Models the same "persisted but not yet reclassified" state as the resume
    test above (review still at expected_version) -- but this time the retry
    proposes a genuinely DIFFERENT account, which must fail closed as a conflict
    rather than silently overwriting or advancing with the new value.
    """

    invoice = _invoice(ettn="P0-PROD-15T-N")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()
    _purpose_use_case(session).execute(_purpose_command(review_id))
    session.commit()

    from app.application.workbench.accounting_resolution import ReviewAccountingResolution

    SqlAlchemyReviewAccountingResolutionRepository(session).create_accounting_resolution(
        ReviewAccountingResolution(
            review_id=review_id,
            company_id=COMPANY_ID,
            review_version=1,
            treatment_type=AccountingTreatmentType.EXPENSE_ACCOUNT,
            expense_account_id=EXPENSE_ACCOUNT_A,
            expense_category="IT_HARDWARE_INTERNAL",
            approved_by=ACTOR,
        )
    )
    session.commit()

    accounting_use_case = _accounting_use_case(session, facts=_ambiguous_facts(invoice))
    with pytest.raises(AccountingResolutionConflictError):
        await accounting_use_case.execute(_accounting_command(review_id, account_id=EXPENSE_ACCOUNT_B))
    assert session.query(WorkbenchReviewAccountingResolution).count() == 1


# =================================================================== 16: resume after persisted-but-not-reclassified


async def test_resume_after_resolution_persisted_but_reclassification_not_completed(session: Session) -> None:
    """Models a crash between the resolution-row commit and the reclassify commit:
    the resolution row exists but the review is still at expected_version. A retry
    of the exact same request must resume reclassification safely -- never a
    second resolution row.
    """

    invoice = _invoice(ettn="P0-PROD-15T-O")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()
    _purpose_use_case(session).execute(_purpose_command(review_id))
    session.commit()

    # Simulate the crash: persist the resolution row directly (as the use case's own
    # fresh-path would, before its second commit), without ever reclassifying.
    from app.application.workbench.accounting_resolution import ReviewAccountingResolution

    accounting_resolution_repository = SqlAlchemyReviewAccountingResolutionRepository(session)
    accounting_resolution_repository.create_accounting_resolution(
        ReviewAccountingResolution(
            review_id=review_id,
            company_id=COMPANY_ID,
            review_version=1,
            treatment_type=AccountingTreatmentType.EXPENSE_ACCOUNT,
            expense_account_id=EXPENSE_ACCOUNT_A,
            expense_category="IT_HARDWARE_INTERNAL",
            approved_by=ACTOR,
        )
    )
    session.commit()
    assert session.query(WorkbenchReviewAccountingResolution).count() == 1
    item = session.scalar(select(WorkbenchReviewItem))
    assert item.version == 1  # reclassification never ran

    accounting_use_case = _accounting_use_case(session, facts=_ambiguous_facts(invoice))
    result = await accounting_use_case.execute(_accounting_command(review_id))
    session.commit()

    assert result.status is AccountingResolutionStatus.RESOLVED
    assert result.current_version == 2
    assert session.query(WorkbenchReviewAccountingResolution).count() == 1  # no duplicate row


# =================================================================== 18, 19: precedence


async def test_review_scoped_resolution_takes_precedence_over_supplier_wide_mapping(session: Session) -> None:
    invoice = _invoice(ettn="P0-PROD-15T-P")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()

    # A supplier-wide mapping ALSO exists for partner 439, pointing at a DIFFERENT account.
    mapping_repository.create(
        company_id=COMPANY_ID,
        vendor_partner_id=CANONICAL_PARTNER_ID,
        expense_account_id=EXPENSE_ACCOUNT_B,
        expense_category="SUPPLIER_WIDE_CATEGORY",
        enabled=True,
    )
    session.commit()

    _purpose_use_case(session).execute(_purpose_command(review_id))
    session.commit()
    accounting_use_case = _accounting_use_case(session, facts=_ambiguous_facts(invoice))
    result = await accounting_use_case.execute(_accounting_command(review_id, account_id=EXPENSE_ACCOUNT_A))
    session.commit()

    assert result.status is AccountingResolutionStatus.RESOLVED
    execution_evidence = session.scalar(
        select(WorkbenchReviewExecutionEvidence).where(WorkbenchReviewExecutionEvidence.review_version == 2)
    )
    if execution_evidence is not None:
        assert execution_evidence.operating_expense_match["expense_account_id"] == EXPENSE_ACCOUNT_A
        assert execution_evidence.operating_expense_match["matched_by"] == "review_accounting_resolution"


async def test_supplier_wide_mapping_still_resolves_without_a_review_scoped_resolution(session: Session) -> None:
    invoice = _invoice(ettn="P0-PROD-15T-Q")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()
    mapping_repository.create(
        company_id=COMPANY_ID,
        vendor_partner_id=CANONICAL_PARTNER_ID,
        expense_account_id=EXPENSE_ACCOUNT_B,
        expense_category="SUPPLIER_WIDE_CATEGORY",
        enabled=True,
    )
    session.commit()

    review_repository = SqlAlchemyReviewRepository(session)
    reclassifier = ReclassifyWorkbenchReviewUseCase(
        decision_engine=_decision_engine(_ambiguous_facts(invoice), mapping_repository=mapping_repository),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        reclassification_writer=review_repository,
        supplier_remediation_effect_reader=effect_repo,
        operating_expense_matcher=OperatingExpenseMatchingEngine(mapping_repository),
        review_accounting_resolution_reader=SqlAlchemyReviewAccountingResolutionRepository(session),
    )
    from app.application.workbench.reclassification import ReclassifyReviewCommand, ReviewReclassificationTrigger

    outcome = await reclassifier.execute(
        ReclassifyReviewCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=1,
            trigger=ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
        )
    )
    session.commit()
    assert "OPERATING_EXPENSE_MAPPING_REQUIRED" not in _reason_codes(outcome.new_review_reasons)


# =================================================================== 20: unrelated reasons preserved


async def test_unrelated_supplier_not_found_reason_is_preserved(session: Session) -> None:
    invoice = _invoice(ettn="P0-PROD-15T-R")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_not_found_facts(invoice), mapping_repository=mapping_repository
    )
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()
    _purpose_use_case(session).execute(_purpose_command(review_id))
    session.commit()

    accounting_use_case = _accounting_use_case(session, facts=_not_found_facts(invoice))
    result = await accounting_use_case.execute(_accounting_command(review_id))
    session.commit()

    codes = _reason_codes(result.current_review_reasons)
    assert "OPERATING_EXPENSE_MAPPING_REQUIRED" not in codes
    assert "SUPPLIER_NOT_FOUND" in codes


# =================================================================== 21: raw evidence stays truthful


async def test_raw_partner_matcher_facts_are_never_mutated(session: Session) -> None:
    invoice = _invoice(ettn="P0-PROD-15T-S")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )
    facts = _ambiguous_facts(invoice)
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()
    _purpose_use_case(session).execute(_purpose_command(review_id))
    session.commit()

    accounting_use_case = _accounting_use_case(session, facts=facts)
    await accounting_use_case.execute(_accounting_command(review_id))
    session.commit()

    assert facts.partner_match.status is PartnerMatchStatus.MULTIPLE_MATCHES
    assert facts.partner_match.candidate_count == 2


# =================================================================== 22: no cross-review leakage (+ acceptance)


async def test_key_acceptance_second_cloudspark_review_stays_blocked_and_no_supplier_mapping_created(
    session: Session,
) -> None:
    """The core P0-PROD-15S/15T requirement: resolving Review A's purchase purpose
    and accounting treatment must never contaminate Review B, an independent,
    identifier-free invoice from the SAME supplier -- and no supplier-wide
    operating_expense_mapping for partner 439 may ever come to exist.
    """

    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)

    invoice_a = _invoice(ettn="P0-PROD-15T-ACCEPT-A")
    review_a = await _import(
        session, invoice=invoice_a, facts=_ambiguous_facts(invoice_a), mapping_repository=mapping_repository
    )
    effect_repo.create_remediation_effect(_effect(review_id=review_a, source_invoice_id=invoice_a.header.ettn))
    session.commit()
    _purpose_use_case(session).execute(_purpose_command(review_a, purpose=PurchasePurpose.INTERNAL_USE))
    session.commit()
    accounting_use_case_a = _accounting_use_case(session, facts=_ambiguous_facts(invoice_a))
    result_a = await accounting_use_case_a.execute(_accounting_command(review_a))
    session.commit()

    assert result_a.status is AccountingResolutionStatus.RESOLVED
    item_a = session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_a))
    assert "OPERATING_EXPENSE_MAPPING_REQUIRED" not in _reason_codes_from_payload(item_a.review_reasons)
    assert item_a.workflow == WorkflowType.VENDOR_BILL.value

    # Independent second review, same supplier, no review-scoped resolution.
    invoice_b = _invoice(ettn="P0-PROD-15T-ACCEPT-B")
    review_b = await _import(
        session, invoice=invoice_b, facts=_ambiguous_facts(invoice_b), mapping_repository=mapping_repository
    )
    item_b = session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_b))
    assert "OPERATING_EXPENSE_MAPPING_REQUIRED" in _reason_codes_from_payload(item_b.review_reasons)
    assert item_b.version == 1

    # The whole point: no supplier-wide mapping was ever created for partner 439.
    assert session.query(OperatingExpenseMappingRecord).count() == 0
    assert session.query(WorkbenchReviewAccountingResolution).count() == 1
    assert session.query(WorkbenchReviewPurchasePurposeResolution).count() == 1


def _reason_codes_from_payload(payload: list[dict]) -> set[str]:
    return {entry["code"] for entry in payload}


# =================================================================== 23: company isolation (resolution reader)


def test_accounting_resolution_reader_is_company_isolated(session: Session) -> None:
    from app.application.workbench.accounting_resolution import ReviewAccountingResolution

    repo = SqlAlchemyReviewAccountingResolutionRepository(session)
    # A row for COMPANY_ID must never leak into a lookup for OTHER_COMPANY_ID, even
    # for a review_id string that happens to be shared/reused across tenants.
    session.add(
        WorkbenchReviewItem(
            review_id="review:shared-id",
            company_id=COMPANY_ID,
            invoice_id="inv-1",
            invoice_number="INV-1",
            supplier_tax_number=VAT,
            supplier_name="CloudSpark",
            invoice_date=date(2026, 9, 8),
            currency="TRY",
            total_amount=Decimal("100.00"),
            workflow="manual_review",
            status="pending_review",
            review_reasons=[],
            warnings=[],
            version=1,
            idempotency_key="uyumsoft:1:inv-1",
        )
    )
    session.flush()
    repo.create_accounting_resolution(
        ReviewAccountingResolution(
            review_id="review:shared-id",
            company_id=COMPANY_ID,
            review_version=1,
            treatment_type=AccountingTreatmentType.EXPENSE_ACCOUNT,
            expense_account_id=EXPENSE_ACCOUNT_A,
            expense_category="OPEX",
            approved_by=ACTOR,
        )
    )
    session.commit()

    assert repo.find_latest_accounting_resolution(review_id="review:shared-id", company_id=COMPANY_ID) is not None
    assert repo.find_latest_accounting_resolution(review_id="review:shared-id", company_id=OTHER_COMPANY_ID) is None


# =================================================================== 24: operating_expense_mappings never written


async def test_accounting_resolution_never_writes_supplier_wide_mapping(session: Session) -> None:
    invoice = _invoice(ettn="P0-PROD-15T-T")
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    review_id = await _import(
        session, invoice=invoice, facts=_ambiguous_facts(invoice), mapping_repository=mapping_repository
    )
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(_effect(review_id=review_id, source_invoice_id=invoice.header.ettn))
    session.commit()
    _purpose_use_case(session).execute(_purpose_command(review_id))
    session.commit()
    accounting_use_case = _accounting_use_case(session, facts=_ambiguous_facts(invoice))
    await accounting_use_case.execute(_accounting_command(review_id))
    session.commit()

    assert session.query(OperatingExpenseMappingRecord).count() == 0


# =================================================================== router: auth, permission, schema hygiene (25)


def _context(*permissions: Permission, company_id: int = COMPANY_ID) -> RequestContext:
    return RequestContext(
        user_id="op-1",
        user_name="Ops One",
        company_id=company_id,
        permissions=permissions,
        trace_id="trace-ppa",
        authentication_method=AuthenticationMethod.JWT,
    )


class _FakePurposeUseCase:
    def __init__(self, *, result: PurchasePurposeSubmissionResult | None = None, error: Exception | None = None):
        self._result = result
        self._error = error
        self.commands: list[SubmitPurchasePurposeCommand] = []

    def execute(self, command: SubmitPurchasePurposeCommand) -> PurchasePurposeSubmissionResult:
        self.commands.append(command)
        if self._error is not None:
            raise self._error
        return self._result  # type: ignore[return-value]


class _FakeAccountingUseCase:
    def __init__(
        self,
        *,
        result: ReviewAccountingResolutionSubmissionResult | None = None,
        error: Exception | None = None,
    ):
        self._result = result
        self._error = error
        self.commands: list[SubmitReviewAccountingResolutionCommand] = []

    async def execute(
        self, command: SubmitReviewAccountingResolutionCommand
    ) -> ReviewAccountingResolutionSubmissionResult:
        self.commands.append(command)
        if self._error is not None:
            raise self._error
        return self._result  # type: ignore[return-value]


def _fake_purpose_result(**kw: Any) -> PurchasePurposeSubmissionResult:
    base: dict[str, Any] = {
        "review_id": "review:endpoint-ppa-1",
        "company_id": COMPANY_ID,
        "review_version": 1,
        "purchase_purpose": PurchasePurpose.INTERNAL_USE,
        "already_applied": False,
        "safe_message": "Purchase purpose recorded. No classification changed.",
    }
    base.update(kw)
    return PurchasePurposeSubmissionResult(**base)


def _fake_accounting_result(**kw: Any) -> ReviewAccountingResolutionSubmissionResult:
    base: dict[str, Any] = {
        "review_id": "review:endpoint-ppa-1",
        "company_id": COMPANY_ID,
        "status": AccountingResolutionStatus.RESOLVED,
        "previous_version": 1,
        "current_version": 2,
        "current_workflow": WorkflowType.VENDOR_BILL,
        "current_review_reasons": (),
        "treatment_type": AccountingTreatmentType.EXPENSE_ACCOUNT,
        "expense_account_id": EXPENSE_ACCOUNT_A,
        "expense_category": "IT_HARDWARE_INTERNAL",
        "reclassified": True,
        "already_applied": False,
        "safe_message": "Accounting resolution recorded; the review was reclassified.",
    }
    base.update(kw)
    return ReviewAccountingResolutionSubmissionResult(**base)


async def _post_purpose(api_client: AsyncClient, *, context: RequestContext, json: dict[str, Any], use_case=None):
    app.dependency_overrides[get_request_context] = lambda: context
    if use_case is not None:
        app.dependency_overrides[get_submit_purchase_purpose_use_case] = lambda: use_case
    try:
        return await api_client.post("/api/workbench/reviews/review:endpoint-ppa-1/purchase-purpose", json=json)
    finally:
        app.dependency_overrides.clear()


async def _post_accounting(api_client: AsyncClient, *, context: RequestContext, json: dict[str, Any], use_case=None):
    app.dependency_overrides[get_request_context] = lambda: context
    if use_case is not None:
        app.dependency_overrides[get_submit_review_accounting_resolution_use_case] = lambda: use_case
    try:
        return await api_client.post("/api/workbench/reviews/review:endpoint-ppa-1/accounting-resolution", json=json)
    finally:
        app.dependency_overrides.clear()


async def test_purchase_purpose_endpoint_happy_path(api_client: AsyncClient) -> None:
    use_case = _FakePurposeUseCase(result=_fake_purpose_result())
    response = await _post_purpose(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={"expected_version": 1, "purchase_purpose": "internal_use"},
        use_case=use_case,
    )
    assert response.status_code == 200
    assert use_case.commands[0].company_id == COMPANY_ID
    assert use_case.commands[0].approved_by == "Ops One"


async def test_purchase_purpose_endpoint_missing_permission_403(api_client: AsyncClient) -> None:
    response = await _post_purpose(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_READ),
        json={"expected_version": 1, "purchase_purpose": "internal_use"},
        use_case=_FakePurposeUseCase(result=_fake_purpose_result()),
    )
    assert response.status_code == 403


async def test_purchase_purpose_endpoint_authentication_failure_401(api_client: AsyncClient) -> None:
    app.dependency_overrides[get_request_context] = lambda: (_ for _ in ()).throw(
        InvalidTokenError("Bearer token is invalid.")
    )
    try:
        response = await api_client.post(
            "/api/workbench/reviews/review:endpoint-ppa-1/purchase-purpose",
            json={"expected_version": 1, "purchase_purpose": "internal_use"},
            headers={"Authorization": "Bearer secret-token", "X-Trace-ID": "trace-401-ppa"},
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 401
    assert "secret-token" not in response.text


async def test_purchase_purpose_endpoint_body_cannot_set_identity_fields(api_client: AsyncClient) -> None:
    response = await _post_purpose(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={
            "expected_version": 1,
            "purchase_purpose": "internal_use",
            "company_id": 999,
            "approved_by": "someone else",
            "source_invoice_id": "forged",
        },
        use_case=_FakePurposeUseCase(result=_fake_purpose_result()),
    )
    assert response.status_code == 400


async def test_accounting_resolution_endpoint_happy_path(api_client: AsyncClient) -> None:
    use_case = _FakeAccountingUseCase(result=_fake_accounting_result())
    response = await _post_accounting(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={
            "expected_version": 1,
            "treatment_type": "expense_account",
            "expense_account_id": EXPENSE_ACCOUNT_A,
            "expense_category": "IT_HARDWARE_INTERNAL",
        },
        use_case=use_case,
    )
    assert response.status_code == 200
    assert use_case.commands[0].company_id == COMPANY_ID
    assert use_case.commands[0].approved_by == "Ops One"


async def test_accounting_resolution_endpoint_rejects_unsupported_treatment_type(api_client: AsyncClient) -> None:
    response = await _post_accounting(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={
            "expected_version": 1,
            "treatment_type": "capitalize_fixed_asset",
            "expense_account_id": EXPENSE_ACCOUNT_A,
            "expense_category": "IT_HARDWARE_INTERNAL",
        },
        use_case=_FakeAccountingUseCase(result=_fake_accounting_result()),
    )
    assert response.status_code == 400


async def test_accounting_resolution_endpoint_missing_permission_403(api_client: AsyncClient) -> None:
    response = await _post_accounting(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_READ),
        json={
            "expected_version": 1,
            "treatment_type": "expense_account",
            "expense_account_id": EXPENSE_ACCOUNT_A,
            "expense_category": "IT_HARDWARE_INTERNAL",
        },
        use_case=_FakeAccountingUseCase(result=_fake_accounting_result()),
    )
    assert response.status_code == 403


async def test_accounting_resolution_endpoint_purpose_unsupported_maps_to_409(api_client: AsyncClient) -> None:
    use_case = _FakeAccountingUseCase(
        error=AccountingResolutionPurposeUnsupportedError("not implemented yet for RESALE")
    )
    response = await _post_accounting(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={
            "expected_version": 1,
            "treatment_type": "expense_account",
            "expense_account_id": EXPENSE_ACCOUNT_A,
            "expense_category": "IT_HARDWARE_INTERNAL",
        },
        use_case=use_case,
    )
    assert response.status_code == 409


async def test_accounting_resolution_endpoint_body_cannot_set_identity_fields(api_client: AsyncClient) -> None:
    response = await _post_accounting(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={
            "expected_version": 1,
            "treatment_type": "expense_account",
            "expense_account_id": EXPENSE_ACCOUNT_A,
            "expense_category": "IT_HARDWARE_INTERNAL",
            "company_id": 999,
            "vendor_partner_id": 439,
        },
        use_case=_FakeAccountingUseCase(result=_fake_accounting_result()),
    )
    assert response.status_code == 400
