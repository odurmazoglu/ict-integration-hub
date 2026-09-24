"""P0-PROD-10D: reclassification consumes an accepted SupplierRemediationEffect
for an archived, Hub-owned ONE_OFF_VENDOR reuse.

Root cause (verified empirically against real production data before this fix):
the generic deterministic PartnerMatchingEngine correctly never considers an
inactive res.partner, so a second invoice from a VAT whose partner was already
retired via the ONE_OFF_VENDOR archive-last lifecycle can never deterministically
re-match that partner -- forever, even after PR #159's writer-level reuse fix
records a durable ``SupplierRemediationEffect``. Because
``review_classification_outcome.build_review_execution_evidence`` requires
``partner_match.status is MATCHED`` for every evidence-producing branch (clean,
operating-expense, and the tolerant "mixed invoice" fallback alike), no Stage-1
execution evidence can ever be pinned, the review's version can never advance
(the writer's own no-op comparison includes ``executable``), and
``SubmitReviewDecisionUseCase`` always raises ``ExecutionSourceInvoiceNotFoundError``.

The fix: ``ReclassifyWorkbenchReviewUseCase`` optionally consults the review's
own, company-scoped ``SupplierRemediationEffect`` -- never any other review's or
company's, never a broadened generic search -- and substitutes a synthesized
``MATCHED`` partner (``matched_by="supplier_remediation_effect"``) *only* for
Stage-1 *execution evidence* construction. The raw deterministic outcome still
drives ``new_workflow``/``new_review_reasons``/classification evidence
unchanged, so the review's own reported reasons remain an honest record of what
the generic matcher actually found. Product resolution needs no equivalent fix:
the existing ``LineResolution.selected_product_id`` / decision-time
substitution already handles an unresolved product line once evidence exists
at all.
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
from app.application.execution.exceptions import ExecutionSourceInvoiceNotFoundError
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
from app.application.workbench.reclassification import ReclassifyReviewCommand, ReviewReclassificationTrigger
from app.application.workbench.selected_product_resolution import ResolutionProductRecord
from app.application.workbench.supplier_remediation import SupplierPartnerWriteEffectStatus, SupplierRemediationEffect
from app.application.workbench.supplier_resolution import SupplierResolutionMode
from app.application.workflow import ManualReviewReasonCode, WorkflowType
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

COMPANY_ID = 1
OTHER_COMPANY_ID = 2
VAT = "2650179910"
ARCHIVED_PARTNER_ID = 448
OTHER_PARTNER_ID = 999
SELLER_ITEM_CODE = "HBCV0000CHXXQ5"
PRODUCT_ID = 389
TAX_ID = 3401
ETTN = "P0-PROD-10D-ETTN"
IDEMPOTENCY_KEY = f"uyumsoft:{COMPANY_ID}:{ETTN}"


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
            WorkbenchReviewDecision.__table__,
            ExecutionSourceInvoiceEvidence.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


# --------------------------------------------------------------------------- builders


def _invoice() -> InternalInvoice:
    """The real P0-PROD-10C production invoice's exact figures (masked ETTN)."""

    return InternalInvoice(
        header=Header(
            invoice_number="HD12026000964602",
            invoice_uuid=ETTN,
            ettn=ETTN,
            issue_date=date(2026, 9, 10),
            currency_code="TRY",
        ),
        supplier=Party(name="D-MARKET ELEKTRONIK HIZMETLER VE TICARET ANONIM SIRKETI", tax_number=VAT),
        customer=Party(name="ICT Teknoloji", tax_number="4651205941"),
        totals=MonetaryTotals(
            line_extension_amount=Decimal("2915.82"),
            tax_exclusive_amount=Decimal("2166.00"),
            tax_inclusive_amount=Decimal("2599.20"),
            allowance_total=Decimal("749.82"),
            payable_amount=Decimal("2599.20"),
        ),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Stanley The Iceflow Flip Straw 2.0 Pipet",
                seller_item_code=SELLER_ITEM_CODE,
                quantity=Decimal("1.000"),
                unit_code="C62",
                unit_price=Decimal("2915.820000"),
                line_extension_amount=Decimal("2915.82"),
                discounts=(Discount(amount=Decimal("749.82")),),
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


class _FakeSelectedProductReader:
    def __init__(self, *, records: tuple[ResolutionProductRecord, ...] = ()) -> None:
        self._records = records

    def find_products_by_ids(self, product_ids: tuple[int, ...]) -> tuple[ResolutionProductRecord, ...]:
        return tuple(r for r in self._records if r.id in product_ids)


class _FakeSupplierRemediationEffectReader:
    """Minimal structural fake of ``SupplierRemediationEffectWriter`` for reads only."""

    def __init__(self, *, effect: SupplierRemediationEffect | None = None) -> None:
        self._effect = effect
        self.calls: list[tuple[str, int]] = []

    def find_latest_remediation_effect(self, *, review_id: str, company_id: int) -> SupplierRemediationEffect | None:
        self.calls.append((review_id, company_id))
        if self._effect is None:
            return None
        if self._effect.review_id != review_id or self._effect.company_id != company_id:
            return None
        return self._effect


def _partner(status: PartnerMatchStatus, *, partner_id: int | None = None) -> PartnerMatchResult:
    matched = status is PartnerMatchStatus.MATCHED
    return PartnerMatchResult(
        status=status,
        partner_id=partner_id if matched else None,
        matched_by="tax_number" if matched else None,
        reason="matched" if matched else "No active deterministic supplier partner candidate found.",
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


def _archived_facts(invoice: InternalInvoice) -> _Facts:
    """The permanent raw deterministic outcome: partner inactive, product unknown."""

    return _Facts(_partner(PartnerMatchStatus.NOT_FOUND), _product_not_found(invoice), _taxes(invoice))


def _active_facts(invoice: InternalInvoice, *, partner_id: int) -> _Facts:
    """A normal, currently-active-partner outcome -- the override must never touch this."""

    return _Facts(
        _partner(PartnerMatchStatus.MATCHED, partner_id=partner_id), _product_not_found(invoice), _taxes(invoice)
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


async def _reclassify(
    session: Session,
    *,
    review_id: str,
    company_id: int = COMPANY_ID,
    expected_version: int,
    rule_result: _Facts,
    remediation_effect_reader: _FakeSupplierRemediationEffectReader | None = None,
    trigger: ReviewReclassificationTrigger = ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
):
    use_case = ReclassifyWorkbenchReviewUseCase(
        decision_engine=_decision_engine(rule_result),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        reclassification_writer=SqlAlchemyReviewRepository(session),
        supplier_remediation_effect_reader=remediation_effect_reader,
    )
    outcome = await use_case.execute(
        ReclassifyReviewCommand(
            review_id=review_id,
            company_id=company_id,
            expected_version=expected_version,
            trigger=trigger,
        )
    )
    session.commit()
    return outcome


def _effect(
    *, review_id: str, company_id: int = COMPANY_ID, partner_id: int = ARCHIVED_PARTNER_ID
) -> SupplierRemediationEffect:
    return SupplierRemediationEffect(
        review_id=review_id,
        company_id=company_id,
        review_version=1,
        source_invoice_id=ETTN,
        mode=SupplierResolutionMode.ONE_OFF_VENDOR,
        resolved_partner_id=partner_id,
        partner_write_status=SupplierPartnerWriteEffectStatus.ALREADY_EXISTS,
    )


def _submit_decision(
    session: Session,
    *,
    review_id: str,
    expected_version: int,
    line_resolutions: tuple[LineResolution, ...],
    product_records: tuple[ResolutionProductRecord, ...] = (),
):
    use_case = SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        selected_product_reader=_FakeSelectedProductReader(records=product_records),
    )
    result = use_case.execute(
        ReviewDecisionCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=expected_version,
            decision=ReviewDecisionType.SELECT_WORKFLOW,
            decided_by="p0-prod-10d-test-operator",
            idempotency_key=f"decision:{review_id}:{expected_version}",
            selected_workflow=WorkflowType.VENDOR_BILL,
            line_resolutions=line_resolutions,
        )
    )
    session.commit()
    return result


def _reason_codes(payload: list[dict]) -> set[str]:
    return {entry["code"] for entry in payload}


def _product_record() -> ResolutionProductRecord:
    return ResolutionProductRecord(
        id=PRODUCT_ID,
        name="Stanley The Iceflow Flip Straw 2.0 Pipet",
        default_code=None,
        barcode=None,
        active=True,
        company_id=COMPANY_ID,
    )


# =================================================================== 1: archived + effect -> executable


async def test_archived_hub_owned_reuse_with_remediation_effect_reaches_executable_evidence(
    session: Session,
) -> None:
    invoice = _invoice()
    review_id = await _import(session, _archived_facts(invoice), invoice=invoice)

    outcome = await _reclassify(
        session,
        review_id=review_id,
        expected_version=1,
        rule_result=_archived_facts(invoice),
        remediation_effect_reader=_FakeSupplierRemediationEffectReader(effect=_effect(review_id=review_id)),
    )

    assert outcome.changed is True
    assert (outcome.from_version, outcome.to_version) == (1, 2)
    assert outcome.executable is True

    # The raw, honest deterministic finding is preserved verbatim -- the generic
    # matcher genuinely still cannot see the inactive partner, and that stays
    # visible. This is not weakened by the fix.
    item = session.scalar(select(WorkbenchReviewItem))
    assert item.version == 2
    assert _reason_codes(item.review_reasons) == {
        ManualReviewReasonCode.SUPPLIER_NOT_FOUND.value,
        ManualReviewReasonCode.PRODUCT_NOT_FOUND.value,
    }

    stage1 = session.scalar(
        select(WorkbenchReviewExecutionEvidence).where(WorkbenchReviewExecutionEvidence.review_version == 2)
    )
    assert stage1 is not None
    assert stage1.partner_match["status"] == PartnerMatchStatus.MATCHED.value
    assert stage1.partner_match["partner_id"] == ARCHIVED_PARTNER_ID
    assert stage1.partner_match["matched_by"] == "supplier_remediation_effect"
    # Product is untouched by the fix -- still the raw NOT_FOUND result.
    assert stage1.product_match["line_results"][0]["result"]["status"] == ProductMatchStatus.NOT_FOUND.value


# =================================================================== 2: archived, no effect -> unchanged fail-closed


async def test_archived_partner_without_remediation_effect_stays_fail_closed(session: Session) -> None:
    invoice = _invoice()
    review_id = await _import(session, _archived_facts(invoice), invoice=invoice)

    outcome = await _reclassify(
        session,
        review_id=review_id,
        expected_version=1,
        rule_result=_archived_facts(invoice),
        remediation_effect_reader=_FakeSupplierRemediationEffectReader(effect=None),
    )

    assert outcome.changed is False
    assert (outcome.from_version, outcome.to_version) == (1, 1)
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 0
    assert session.scalar(select(WorkbenchReviewItem)).version == 1


async def test_archived_partner_with_no_reader_wired_at_all_stays_fail_closed(session: Session) -> None:
    """Identical to today's behavior when no composition root opts in (reader=None)."""

    invoice = _invoice()
    review_id = await _import(session, _archived_facts(invoice), invoice=invoice)

    outcome = await _reclassify(session, review_id=review_id, expected_version=1, rule_result=_archived_facts(invoice))

    assert outcome.changed is False
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 0


# =================================================================== 3: normal active partner -> unchanged


async def test_normal_active_partner_reclassification_is_unaffected_by_remediation_effect_reader(
    session: Session,
) -> None:
    invoice = _invoice()
    review_id = await _import(session, _archived_facts(invoice), invoice=invoice)

    # A genuinely active partner match -- the reader is wired in AND has an effect
    # on file, but since partner_match is already MATCHED, the override must never
    # even be consulted.
    reader = _FakeSupplierRemediationEffectReader(effect=_effect(review_id=review_id, partner_id=OTHER_PARTNER_ID))
    outcome = await _reclassify(
        session,
        review_id=review_id,
        expected_version=1,
        rule_result=_active_facts(invoice, partner_id=OTHER_PARTNER_ID),
        remediation_effect_reader=reader,
    )

    assert reader.calls == []  # never consulted -- raw match already succeeded
    assert outcome.changed is True
    stage1 = session.scalar(
        select(WorkbenchReviewExecutionEvidence).where(WorkbenchReviewExecutionEvidence.review_version == 2)
    )
    assert stage1.partner_match["partner_id"] == OTHER_PARTNER_ID
    assert stage1.partner_match["matched_by"] == "tax_number"  # the real match, not the override


# =================================================================== 4: cross-review / cross-company isolation


async def test_remediation_effect_for_a_different_review_is_never_consumed(session: Session) -> None:
    invoice = _invoice()
    review_id = await _import(session, _archived_facts(invoice), invoice=invoice)

    reader = _FakeSupplierRemediationEffectReader(effect=_effect(review_id="review:some-other-review"))
    outcome = await _reclassify(
        session,
        review_id=review_id,
        expected_version=1,
        rule_result=_archived_facts(invoice),
        remediation_effect_reader=reader,
    )

    assert outcome.changed is False
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 0


async def test_remediation_effect_for_a_different_company_is_never_consumed(session: Session) -> None:
    invoice = _invoice()
    review_id = await _import(session, _archived_facts(invoice), invoice=invoice)

    reader = _FakeSupplierRemediationEffectReader(effect=_effect(review_id=review_id, company_id=OTHER_COMPANY_ID))
    outcome = await _reclassify(
        session,
        review_id=review_id,
        expected_version=1,
        rule_result=_archived_facts(invoice),
        remediation_effect_reader=reader,
    )

    assert outcome.changed is False
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 0


# =================================================================== 5: full regression


async def test_full_regression_archived_reuse_reclassify_and_product_based_decision_reach_decision_submitted(
    session: Session,
) -> None:
    """Reproduces the exact P0-PROD-10C production sequence end to end, with the
    upstream remediation writes (ONE_OFF_VENDOR reuse of an archived partner,
    CREATE_NEW_PRODUCT) represented by their real, durable persisted effects --
    exactly what those already-separately-tested use cases (#159/#163/#164)
    would have produced -- proving the new reclassification behavior is what
    unblocks the previously-reproduced ``ExecutionSourceInvoiceNotFoundError``.
    """

    invoice = _invoice()
    review_id = await _import(session, _archived_facts(invoice), invoice=invoice)

    # Before the fix: reclassifying with no override reproduces the exact
    # production blocker.
    before = await _reclassify(session, review_id=review_id, expected_version=1, rule_result=_archived_facts(invoice))
    assert before.changed is False
    with pytest.raises(ExecutionSourceInvoiceNotFoundError):
        _submit_decision(
            session,
            review_id=review_id,
            expected_version=1,
            line_resolutions=(LineResolution(line_number="1", selected_product_id=PRODUCT_ID),),
            product_records=(_product_record(),),
        )
    session.rollback()

    # The durable, already-recorded remediation effect (mirrors real production:
    # ONE_OFF_VENDOR reuse of archived Hub-owned partner 448).
    reader = _FakeSupplierRemediationEffectReader(effect=_effect(review_id=review_id))

    outcome = await _reclassify(
        session,
        review_id=review_id,
        expected_version=1,
        rule_result=_archived_facts(invoice),
        remediation_effect_reader=reader,
    )
    assert outcome.changed is True
    assert outcome.executable is True

    result = _submit_decision(
        session,
        review_id=review_id,
        expected_version=2,
        line_resolutions=(LineResolution(line_number="1", selected_product_id=PRODUCT_ID),),
        product_records=(_product_record(),),
    )

    item = session.scalar(select(WorkbenchReviewItem))
    assert item.status == "decision_submitted"
    assert item.version == 3

    stage2 = SqlAlchemyExecutionSourceInvoiceReader(session).get_source_invoice(
        review_id=review_id, company_id=COMPANY_ID, decision_version=3
    )
    assert stage2.partner_match.partner_id == ARCHIVED_PARTNER_ID
    assert stage2.product_match.line_results[0].result.product_id == PRODUCT_ID
    assert stage2.invoice.totals.tax_exclusive_amount == Decimal("2166.00")
    assert stage2.invoice.totals.payable_amount == Decimal("2599.20")
    assert result.accepted is True
    assert result.status.value == "decision_submitted"
