"""P0-PROD-15N: an accepted MATCH_EXISTING SupplierRemediationEffect must become
authoritative for the review's own reclassification, not just for Stage-1
execution evidence.

Root cause (proven against real production after P0-PROD-15L deployed):
``ResolveWorkbenchSupplierUseCase`` durably records the operator's MATCH_EXISTING
selection, then triggers ``ReclassifyWorkbenchReviewUseCase``. That use case
reruns the raw deterministic ``PartnerMatchingEngine`` against live Odoo data.
Since MATCH_EXISTING never mutates Odoo (unlike CREATE_PERMANENT_SUPPLIER /
ONE_OFF_VENDOR, whose newly-activated partner the raw matcher then finds on its
own), Odoo still truthfully contains the same multiple exact-VAT candidates it
always had, so the raw matcher reproduces SUPPLIER_AMBIGUOUS forever -- the
review can never actually reclassify past it, no matter how deterministic the
operator's selection was. Confirmed against review:b9aacadc-c67e-50b2-9183-
bb730cb4709b (CloudSpark, partner 439 vs. child contact 440): the supported
POST /supplier-resolution returned HTTP 200 "resolved" while
reclassified=false and SUPPLIER_AMBIGUOUS was still present.

The fix (``app/application/use_cases/reclassify_review.py``,
``_effective_manual_review_reasons`` / ``_effective_workflow``): an accepted
MATCH_EXISTING effect is folded into the review's *effective* classification --
SUPPLIER_AMBIGUOUS is stripped from the persisted ``new_review_reasons`` and
the workflow re-derived -- while the raw ``classification_evidence`` (and the
raw PartnerMatchResult the matcher itself produced) stays an untouched, honest
record of what the generic matcher actually found. Every other reason,
including SUPPLIER_NOT_FOUND, is completely unaffected, and the fix never
broadens which modes are eligible (still exactly MATCH_EXISTING, per
P0-PROD-15L).

A second, narrower defect in the same defect report
(``app/application/workbench/supplier_remediation_use_cases.py``,
``_result_from_reclass``) is fixed alongside it: the HTTP response must never
claim "resolved; the review was reclassified" while an actionable supplier
blocker (SUPPLIER_NOT_FOUND, or for MATCH_EXISTING, SUPPLIER_AMBIGUOUS) is
still present in the reclassified reasons.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands import ImportInvoiceCommand
from app.application.commands.supplier_partner import CreateSupplierPartnerCommand
from app.application.decision import (
    DecisionEngine,
    ManualReviewStrategy,
    VendorBillReviewRecommendationStrategy,
    WorkflowStrategyResolver,
)
from app.application.dto.supplier_partner import SupplierPartnerWriteResult
from app.application.rules.deterministic import DeterministicRuleEngine
from app.application.use_cases import ImportInvoiceUseCase
from app.application.use_cases.reclassify_review import ReclassifyWorkbenchReviewUseCase
from app.application.workbench import ReviewItemCreationService
from app.application.workbench.exceptions import SupplierResolutionPartnerMismatchError
from app.application.workbench.reclassification import ReclassifyReviewCommand, ReviewReclassificationTrigger
from app.application.workbench.supplier_remediation import (
    ResolveWorkbenchSupplierCommand,
    SupplierPartnerWriteEffectStatus,
    SupplierRemediationEffect,
    SupplierRemediationStatus,
)
from app.application.workbench.supplier_remediation_use_cases import ResolveWorkbenchSupplierUseCase
from app.application.workbench.supplier_resolution import ResolutionPartnerRecord, SupplierResolutionMode
from app.application.workbench.supplier_resolution_use_cases import ValidateSupplierResolutionUseCase
from app.application.workflow import ManualReviewReasonCode, WorkflowType
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.models.workbench_review_classification_evidence import WorkbenchReviewClassificationEvidence
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_one_off_vendor_retirement import WorkbenchReviewOneOffVendorRetirement
from app.models.workbench_review_reclassification import WorkbenchReviewReclassification
from app.models.workbench_review_source_invoice_evidence import WorkbenchReviewSourceInvoiceEvidence
from app.models.workbench_review_supplier_remediation_effect import WorkbenchReviewSupplierRemediationEffect
from app.models.workbench_review_supplier_resolution import WorkbenchReviewSupplierResolution
from app.persistence import (
    SqlAlchemyReviewRepository,
    SqlAlchemyReviewSourceInvoiceEvidenceReader,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyReviewSupplierResolutionRepository,
    SqlAlchemyUnitOfWork,
)
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType

COMPANY_ID = 1
OTHER_COMPANY_ID = 2
VAT = "1760390647"
CANONICAL_PARTNER_ID = 439
CHILD_CONTACT_PARTNER_ID = 440
OTHER_PARTNER_ID = 999
TAX_ID = 8801
ETTN = "P0-PROD-15N-ETTN"
IDEMPOTENCY_KEY = f"uyumsoft:{COMPANY_ID}:{ETTN}"
ACTOR = "p0-prod-15n-test-operator"


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
            WorkbenchReviewSupplierResolution.__table__,
            WorkbenchReviewSupplierRemediationEffect.__table__,
            WorkbenchReviewOneOffVendorRetirement.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


# --------------------------------------------------------------------------- builders (self-contained)


def _invoice(*, seller_item_code: str | None = None) -> InternalInvoice:
    """Shaped exactly like the real CloudSpark production invoice: 20% KDV,
    identifier-free lines (no seller/buyer item code), no operating-expense
    mapping configured -- the raw deterministic outcome is therefore
    (SUPPLIER_AMBIGUOUS, OPERATING_EXPENSE_MAPPING_REQUIRED), exactly like
    review:b9aacadc-c67e-50b2-9183-bb730cb4709b.
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
                seller_item_code=seller_item_code,
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


def _ambiguous_facts(invoice: InternalInvoice, *, candidate_count: int = 2) -> _Facts:
    """The permanent raw deterministic outcome: >=2 active exact-VAT candidates.

    MATCH_EXISTING never mutates Odoo, so this stays true before *and* after an
    accepted remediation effect -- the raw matcher's own finding never changes.
    """

    return _Facts(_partner_ambiguous(candidate_count=candidate_count), _product_not_found(invoice), _taxes(invoice))


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
    trigger: ReviewReclassificationTrigger = ReviewReclassificationTrigger.SUPPLIER_RESOLUTION,
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
    *,
    review_id: str,
    company_id: int = COMPANY_ID,
    review_version: int = 1,
    partner_id: int = CANONICAL_PARTNER_ID,
) -> SupplierRemediationEffect:
    return SupplierRemediationEffect(
        review_id=review_id,
        company_id=company_id,
        review_version=review_version,
        source_invoice_id=ETTN,
        mode=SupplierResolutionMode.MATCH_EXISTING,
        resolved_partner_id=partner_id,
        partner_write_status=SupplierPartnerWriteEffectStatus.SELECTED,
    )


def _reason_codes(payload: list[dict]) -> set[str]:
    return {entry["code"] for entry in payload}


# =================================================================== A: SUPPLIER_AMBIGUOUS + MATCH_EXISTING resolves


async def test_match_existing_effect_clears_supplier_ambiguous_but_keeps_other_reason(session: Session) -> None:
    invoice = _invoice()
    review_id = await _import(session, _ambiguous_facts(invoice), invoice=invoice)

    item = session.scalar(select(WorkbenchReviewItem))
    assert _reason_codes(item.review_reasons) == {
        ManualReviewReasonCode.SUPPLIER_AMBIGUOUS.value,
        ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED.value,
    }

    outcome = await _reclassify(
        session,
        review_id=review_id,
        expected_version=1,
        rule_result=_ambiguous_facts(invoice),
        remediation_effect_reader=_FakeSupplierRemediationEffectReader(effect=_effect(review_id=review_id)),
    )

    assert outcome.changed is True
    assert (outcome.from_version, outcome.to_version) == (1, 2)
    assert _reason_codes([_ser(r) for r in outcome.new_review_reasons]) == {
        ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED.value
    }
    # Still Manual Review: an independent reason remains, so the workflow must
    # not silently become VENDOR_BILL.
    assert outcome.new_workflow is WorkflowType.MANUAL_REVIEW

    item = session.scalar(select(WorkbenchReviewItem))
    assert item.version == 2
    assert _reason_codes(item.review_reasons) == {ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED.value}
    assert item.workflow == WorkflowType.MANUAL_REVIEW.value


def _ser(reason) -> dict:
    return {"code": reason.code.value}


# =================================================================== B: raw evidence stays truthful


async def test_raw_partner_matcher_facts_are_never_mutated_by_the_fix(session: Session) -> None:
    """The exact same ambiguous-candidate fixture drives both calls below -- the
    fix never reaches into or reconfigures the raw matcher/fixture; it only
    changes what the *review* ends up reporting.
    """

    invoice = _invoice()
    review_id = await _import(session, _ambiguous_facts(invoice), invoice=invoice)
    facts = _ambiguous_facts(invoice)

    # Before: reclassifying with the raw matcher and no accepted effect reproduces
    # the exact production blocker (proves the bug, using the unmodified fixture).
    before = await _reclassify(session, review_id=review_id, expected_version=1, rule_result=facts)
    assert before.changed is False
    assert facts.partner_match.status is PartnerMatchStatus.MULTIPLE_MATCHES
    assert facts.partner_match.candidate_count == 2

    # After: the identical `facts` object/fixture is reused verbatim -- only an
    # accepted remediation effect is added.
    after = await _reclassify(
        session,
        review_id=review_id,
        expected_version=1,
        rule_result=facts,
        remediation_effect_reader=_FakeSupplierRemediationEffectReader(effect=_effect(review_id=review_id)),
    )
    assert after.changed is True
    assert facts.partner_match.status is PartnerMatchStatus.MULTIPLE_MATCHES
    assert facts.partner_match.candidate_count == 2

    # This exact CloudSpark-shaped, identifier-free invoice with an unmatched
    # operating-expense mapping never reaches Stage-1 execution evidence at all
    # (matches real production: no executable evidence until the expense mapping
    # is itself configured) -- the fix only changes the review's own effective
    # classification reasons, never manufactures execution evidence.
    stage1 = session.scalar(
        select(WorkbenchReviewExecutionEvidence).where(WorkbenchReviewExecutionEvidence.review_version == 2)
    )
    assert stage1 is None


# =================================================================== C: no over-clearing


async def test_unrelated_reason_is_never_removed_by_the_supplier_effect(session: Session) -> None:
    invoice = _invoice()
    review_id = await _import(session, _ambiguous_facts(invoice), invoice=invoice)

    outcome = await _reclassify(
        session,
        review_id=review_id,
        expected_version=1,
        rule_result=_ambiguous_facts(invoice),
        remediation_effect_reader=_FakeSupplierRemediationEffectReader(effect=_effect(review_id=review_id)),
    )

    codes = {r.code for r in outcome.new_review_reasons}
    assert ManualReviewReasonCode.SUPPLIER_AMBIGUOUS not in codes
    assert ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED in codes


# =================================================================== D: scope isolation


async def test_effect_for_a_different_review_never_resolves_this_reviews_ambiguity(session: Session) -> None:
    invoice = _invoice()
    review_id = await _import(session, _ambiguous_facts(invoice), invoice=invoice)

    reader = _FakeSupplierRemediationEffectReader(effect=_effect(review_id="review:some-other-review"))
    outcome = await _reclassify(
        session,
        review_id=review_id,
        expected_version=1,
        rule_result=_ambiguous_facts(invoice),
        remediation_effect_reader=reader,
    )

    assert outcome.changed is False
    assert ManualReviewReasonCode.SUPPLIER_AMBIGUOUS in {r.code for r in outcome.new_review_reasons}


async def test_effect_for_a_different_company_never_resolves_this_reviews_ambiguity(session: Session) -> None:
    invoice = _invoice()
    review_id = await _import(session, _ambiguous_facts(invoice), invoice=invoice)

    reader = _FakeSupplierRemediationEffectReader(effect=_effect(review_id=review_id, company_id=OTHER_COMPANY_ID))
    outcome = await _reclassify(
        session,
        review_id=review_id,
        expected_version=1,
        rule_result=_ambiguous_facts(invoice),
        remediation_effect_reader=reader,
    )

    assert outcome.changed is False
    assert ManualReviewReasonCode.SUPPLIER_AMBIGUOUS in {r.code for r in outcome.new_review_reasons}


# =================================================================== F: SUPPLIER_NOT_FOUND unaffected


async def test_supplier_not_found_review_reasons_are_never_touched_by_the_fix(session: Session) -> None:
    invoice = _invoice()
    not_found_facts = _Facts(
        PartnerMatchResult(
            status=PartnerMatchStatus.NOT_FOUND,
            partner_id=None,
            matched_by=None,
            reason="No active deterministic supplier partner candidate found.",
            candidate_count=0,
            confidence=None,
        ),
        _product_not_found(invoice),
        _taxes(invoice),
    )
    review_id = await _import(session, not_found_facts, invoice=invoice)

    # Even with a (mode-mismatched-on-purpose) effect on file, SUPPLIER_NOT_FOUND
    # must never be stripped by this fix -- only MATCH_EXISTING + SUPPLIER_AMBIGUOUS is.
    effect = _effect(review_id=review_id)  # MATCH_EXISTING, but raw reason is NOT_FOUND, not AMBIGUOUS
    outcome = await _reclassify(
        session,
        review_id=review_id,
        expected_version=1,
        rule_result=not_found_facts,
        remediation_effect_reader=_FakeSupplierRemediationEffectReader(effect=effect),
    )

    assert ManualReviewReasonCode.SUPPLIER_NOT_FOUND in {r.code for r in outcome.new_review_reasons}


# =================================================================== G: CREATE_PERMANENT_SUPPLIER / ONE_OFF unaffected


async def test_one_off_vendor_effect_never_strips_supplier_ambiguous(session: Session) -> None:
    """P0-PROD-15L intentionally kept CREATE_PERMANENT_SUPPLIER/ONE_OFF_VENDOR
    restricted to SUPPLIER_NOT_FOUND; this fix must not silently broaden them to
    SUPPLIER_AMBIGUOUS either, even if an (illegitimate) effect existed.
    """

    invoice = _invoice()
    review_id = await _import(session, _ambiguous_facts(invoice), invoice=invoice)

    effect = SupplierRemediationEffect(
        review_id=review_id,
        company_id=COMPANY_ID,
        review_version=1,
        source_invoice_id=ETTN,
        mode=SupplierResolutionMode.ONE_OFF_VENDOR,
        resolved_partner_id=CANONICAL_PARTNER_ID,
        partner_write_status=SupplierPartnerWriteEffectStatus.ALREADY_EXISTS,
    )
    outcome = await _reclassify(
        session,
        review_id=review_id,
        expected_version=1,
        rule_result=_ambiguous_facts(invoice),
        remediation_effect_reader=_FakeSupplierRemediationEffectReader(effect=effect),
    )

    assert ManualReviewReasonCode.SUPPLIER_AMBIGUOUS in {r.code for r in outcome.new_review_reasons}


# =================================================================== E, H, I: full orchestration (real use case)


class _FakePartnerReader:
    def __init__(self, *records: ResolutionPartnerRecord) -> None:
        self._by_id = {r.id: r for r in records}
        self.calls: list[int] = []

    def find_partner_by_id(self, partner_id: int) -> ResolutionPartnerRecord | None:
        self.calls.append(partner_id)
        return self._by_id.get(partner_id)


class _UnreachableSupplierPartnerWriter:
    """MATCH_EXISTING must never reach the partner writer at all."""

    async def create_supplier(self, command: CreateSupplierPartnerCommand) -> SupplierPartnerWriteResult:
        raise AssertionError("MATCH_EXISTING must never call the supplier partner writer.")


def _canonical_partner(*, id_: int = CANONICAL_PARTNER_ID) -> ResolutionPartnerRecord:
    return ResolutionPartnerRecord(id=id_, name="CloudSpark", vat=VAT, active=True, company_id=None)


def _use_case(
    session: Session, *, rule_result: _Facts, partner: ResolutionPartnerRecord
) -> ResolveWorkbenchSupplierUseCase:
    review_reader = SqlAlchemyReviewRepository(session)
    return ResolveWorkbenchSupplierUseCase(
        review_reader=review_reader,
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        resolution_validator=ValidateSupplierResolutionUseCase(
            source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
            partner_reader=_FakePartnerReader(partner),
        ),
        resolution_writer=SqlAlchemyReviewSupplierResolutionRepository(session),
        remediation_effect_writer=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        supplier_partner_writer=_UnreachableSupplierPartnerWriter(),
        reclassifier=ReclassifyWorkbenchReviewUseCase(
            decision_engine=_decision_engine(rule_result),
            source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
            reclassification_writer=SqlAlchemyReviewRepository(session),
            supplier_remediation_effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        ),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


def _command(
    review_id: str, *, expected_version: int = 1, partner_id: int = CANONICAL_PARTNER_ID
) -> ResolveWorkbenchSupplierCommand:
    return ResolveWorkbenchSupplierCommand(
        review_id=review_id,
        company_id=COMPANY_ID,
        expected_version=expected_version,
        mode=SupplierResolutionMode.MATCH_EXISTING,
        approved_by=ACTOR,
        resolved_partner_id=partner_id,
    )


async def test_full_orchestration_resolves_ambiguity_end_to_end(session: Session) -> None:
    """The exact production sequence: POST supplier-resolution against a
    SUPPLIER_AMBIGUOUS + OPERATING_EXPENSE_MAPPING_REQUIRED review, partner 439
    selected. Must now actually resolve, not silently no-op.
    """

    invoice = _invoice()
    review_id = await _import(session, _ambiguous_facts(invoice), invoice=invoice)
    use_case = _use_case(session, rule_result=_ambiguous_facts(invoice), partner=_canonical_partner())

    result = await use_case.execute(_command(review_id))
    session.commit()

    assert result.status is SupplierRemediationStatus.RESOLVED
    assert result.reclassified is True
    assert result.already_applied is False
    assert result.current_version == 2
    assert result.effective_partner_id == CANONICAL_PARTNER_ID
    codes = {r.code for r in result.current_review_reasons}
    assert ManualReviewReasonCode.SUPPLIER_AMBIGUOUS not in codes
    assert ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED in codes
    assert "Supplier resolved; the review was reclassified." in (result.safe_message or "")

    # Exactly one remediation effect was recorded -- no duplicate.
    effects = session.query(WorkbenchReviewSupplierRemediationEffect).all()
    assert len(effects) == 1
    assert effects[0].resolved_partner_id == CANONICAL_PARTNER_ID


# =================================================================== E: invalid selection still fails closed


async def test_wrong_vat_partner_still_fails_closed_when_ambiguous(session: Session) -> None:
    invoice = _invoice()
    review_id = await _import(session, _ambiguous_facts(invoice), invoice=invoice)
    wrong_vat_partner = ResolutionPartnerRecord(
        id=OTHER_PARTNER_ID, name="Someone Else", vat="9999999999", active=True, company_id=None
    )
    use_case = _use_case(session, rule_result=_ambiguous_facts(invoice), partner=wrong_vat_partner)

    with pytest.raises(SupplierResolutionPartnerMismatchError):
        await use_case.execute(_command(review_id, partner_id=OTHER_PARTNER_ID))

    # No effect was ever recorded for a rejected selection.
    assert session.query(WorkbenchReviewSupplierRemediationEffect).count() == 0
    item = session.scalar(select(WorkbenchReviewItem))
    assert item.version == 1


# =================================================================== H: idempotent resume from a persisted effect


async def test_resume_from_a_previously_persisted_effect_reaches_resolved_without_a_duplicate_write(
    session: Session,
) -> None:
    """Models the real production situation after the first (pre-fix) HTTP 200:
    the SupplierRemediationEffect (and the underlying SupplierResolution intent)
    were already durably committed, but the review itself never advanced because
    reclassification kept reproducing the same ambiguity. A resume of the exact
    same supported request must now (post-fix) actually reach RESOLVED, reusing
    the existing effect -- no duplicate effect, no duplicate Odoo write.
    """

    invoice = _invoice()
    review_id = await _import(session, _ambiguous_facts(invoice), invoice=invoice)
    use_case = _use_case(session, rule_result=_ambiguous_facts(invoice), partner=_canonical_partner())

    # First attempt: pre-fix stand-in would have committed the intent + effect but
    # left the review at version 1 forever. Here it runs against the FIXED
    # reclassifier and so already resolves on this first call -- proving the
    # normal path works before we separately model a stuck resume below.
    first = await use_case.execute(_command(review_id))
    session.commit()
    assert first.status is SupplierRemediationStatus.RESOLVED
    assert first.current_version == 2

    # A supported retry of the exact same logical request (same mode/partner/
    # version) must be recognized as already applied -- no duplicate effect, no
    # second call into the partner writer (which would raise), no version drift.
    second = await use_case.execute(_command(review_id))
    session.commit()

    assert second.already_applied is True
    assert second.status is SupplierRemediationStatus.RESOLVED
    assert second.current_version == 2
    effects = session.query(WorkbenchReviewSupplierRemediationEffect).all()
    assert len(effects) == 1
    resolutions = session.query(WorkbenchReviewSupplierResolution).all()
    assert len(resolutions) == 1


# =================================================================== I: response truthfulness


class _FakeStuckReclassifier:
    """Simulates the exact pre-fix production bug: reclassification runs, but
    since the raw matcher still reports ambiguity and nothing consults the
    accepted effect, nothing actually changes.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, command: ReclassifyReviewCommand):
        from app.application.workbench.reclassification import ReviewReclassificationResult
        from app.application.workflow import ManualReviewReason

        self.calls += 1
        still_ambiguous = (
            ManualReviewReason(
                code=ManualReviewReasonCode.SUPPLIER_AMBIGUOUS,
                message="Supplier match is ambiguous.",
                source="partner_matching",
                candidate_count=2,
            ),
            ManualReviewReason(
                code=ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED,
                message="No enabled operating-expense mapping is configured for this supplier.",
                source="operating_expense_matching",
            ),
        )
        return ReviewReclassificationResult(
            review_id=command.review_id,
            company_id=command.company_id,
            changed=False,
            from_version=command.expected_version,
            to_version=command.expected_version,
            previous_workflow=WorkflowType.MANUAL_REVIEW,
            new_workflow=WorkflowType.MANUAL_REVIEW,
            previous_review_reasons=still_ambiguous,
            new_review_reasons=still_ambiguous,
            trigger=command.trigger,
            executable=False,
        )


async def test_response_never_claims_resolved_while_supplier_ambiguous_persists(session: Session) -> None:
    review_reader = SqlAlchemyReviewRepository(session)
    invoice = _invoice()
    review_id = await _import(session, _ambiguous_facts(invoice), invoice=invoice)

    stuck_reclassifier = _FakeStuckReclassifier()
    use_case = ResolveWorkbenchSupplierUseCase(
        review_reader=review_reader,
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        resolution_validator=ValidateSupplierResolutionUseCase(
            source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
            partner_reader=_FakePartnerReader(_canonical_partner()),
        ),
        resolution_writer=SqlAlchemyReviewSupplierResolutionRepository(session),
        remediation_effect_writer=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        supplier_partner_writer=_UnreachableSupplierPartnerWriter(),
        reclassifier=stuck_reclassifier,
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )

    result = await use_case.execute(_command(review_id))
    session.commit()

    assert stuck_reclassifier.calls == 1
    # The defect: reclassified is false and SUPPLIER_AMBIGUOUS is still present.
    assert result.reclassified is False
    assert ManualReviewReasonCode.SUPPLIER_AMBIGUOUS in {r.code for r in result.current_review_reasons}
    # The fix under test: status/message must not claim success in this state.
    assert result.status is SupplierRemediationStatus.REMEDIATION_INCOMPLETE
    assert "resolved; the review was reclassified" not in (result.safe_message or "").lower()


# =================================================================== J: optimistic concurrency preserved


async def test_stale_expected_version_still_conflicts_when_ambiguous(session: Session) -> None:
    from app.application.workbench.exceptions import ReviewVersionConflictError

    invoice = _invoice()
    review_id = await _import(session, _ambiguous_facts(invoice), invoice=invoice)
    use_case = _use_case(session, rule_result=_ambiguous_facts(invoice), partner=_canonical_partner())

    with pytest.raises(ReviewVersionConflictError):
        await use_case.execute(_command(review_id, expected_version=99))

    assert session.query(WorkbenchReviewSupplierRemediationEffect).count() == 0
