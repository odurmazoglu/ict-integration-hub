"""PR A -- effective supplier foundation + current #208 mapping hardening.

The defect (traced at e58b2c3): an accepted ``MATCH_EXISTING -> partner X`` reached
execution (``EffectiveDecisionResolver._execution_decision_result`` substituted X into
Stage-1 evidence) and product remediation (``MapExistingProductUseCase`` read X back from
that evidence and wrote supplierinfo for X), but reclassification ran the DecisionEngine
with the *raw* partner match. ``DeterministicRuleEngine.evaluate`` then handed
``ProductMatchingEngine`` a MULTIPLE_MATCHES partner, ``_resolved_supplier_partner_id``
returned None, supplierinfo never participated, and the mapped line stayed
PRODUCT_NOT_FOUND forever.

Everything below runs the REAL ``PartnerMatchingEngine`` (#206 canonicalization), the
REAL ``ProductMatchingEngine`` (#209 semantics) and the REAL reclassification over SQLite,
against an in-memory Odoo.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands import ImportInvoiceCommand
from app.application.decision import (
    DecisionEngine,
    ManualReviewStrategy,
    VendorBillReviewRecommendationStrategy,
    WorkflowStrategyResolver,
)
from app.application.effective_supplier import (
    ACCEPTED_SUPPLIER_MATCHED_BY,
    AcceptedSupplier,
    AcceptedSupplierReader,
    AcceptedSupplierStatus,
    EffectiveSupplierOrigin,
    EffectiveSupplierResolver,
    resolve_effective_supplier,
    supplier_match_for_product_matching,
)
from app.application.rules.deterministic import DeterministicRuleEngine
from app.application.use_cases import ImportInvoiceUseCase
from app.application.use_cases.effective_decision import EffectiveDecisionResolver
from app.application.use_cases.reclassify_review import ReclassifyWorkbenchReviewUseCase
from app.application.workbench import ReviewItemCreationService
from app.application.workbench.dto import ReviewItem, ReviewStatus
from app.application.workbench.exceptions import (
    EffectiveSupplierReadError,
    ProductRemediationSupplierUnresolvedError,
    ReviewNotFoundError,
    WorkbenchContractError,
)
from app.application.workbench.operator_guidance import (
    PRODUCT_LABEL_UNAVAILABLE,
    GuidanceInput,
    OperatorGuidanceFacts,
    ResolvedProductLine,
    build_operator_guidance,
)
from app.application.workbench.operator_request_ingestion import (
    OperatorActionOutcome,
    OperatorRequestAction,
    OperatorRequestOutcome,
)
from app.application.workbench.product_mapping import MapExistingProductCommand
from app.application.workbench.product_mapping_use_cases import MapExistingProductUseCase
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.reclassification import ReclassifyReviewCommand, ReviewReclassificationTrigger
from app.application.workbench.supplier_remediation import SupplierPartnerWriteEffectStatus, SupplierRemediationEffect
from app.application.workbench.supplier_resolution import SupplierResolutionMode
from app.application.workflow import ManualReviewReasonCode, WorkflowType
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.erp.models import Partner
from app.erp.odoo.existing_supplier_info_reader import OdooExistingSupplierInfoReader
from app.erp.odoo.partner_repository import OdooPartnerRepository
from app.erp.odoo.workbench_operator_request_reader import (
    OdooOperatorRequestAcknowledger,
    OdooOperatorRequestFieldMapping,
)
from app.erp.write.odoo_product_write_policy import OdooProductWritePolicy
from app.erp.write.odoo_supplierinfo_writer import OdooSupplierInfoRepository, OdooSupplierInfoWriter
from app.matching import PartnerMatchingEngine, ProductMatchingEngine, ProductMatchStatus
from app.matching.exceptions import PartnerMatchingError
from app.models.workbench_review_classification_evidence import WorkbenchReviewClassificationEvidence
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_reclassification import WorkbenchReviewReclassification
from app.models.workbench_review_source_invoice_correction import WorkbenchReviewSourceInvoiceCorrection
from app.models.workbench_review_source_invoice_evidence import WorkbenchReviewSourceInvoiceEvidence
from app.persistence import (
    SqlAlchemyReviewRepository,
    SqlAlchemyReviewSourceInvoiceEvidenceReader,
    SqlAlchemyUnitOfWork,
)
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType
from tests.unit.effective_supplier_support import (
    CanonicalPartners,
    FixedPartnerMatcher,
    contact,
    head,
    raw_ambiguous,
    raw_matched,
    raw_not_found,
)
from tests.unit.test_adr_0013_operator_request_ingestion import (
    REQUESTED_AT,
    FakeAcknowledger,
    FakeJson2,
    FakeReader,
    ScriptedHandler,
    _request,
    _request_mapping,
    _row,
    _workflow,
)
from tests.unit.test_product_not_found_operator_flow import InMemoryOdoo, _NoGlobalProducts

COMPANY = 1
VAT = "1760390647"
PARTNER_X = 439  # the operator's MATCH_EXISTING choice
PARTNER_OTHER = 777  # a second, genuinely distinct commercial head with the same VAT
ICT_BULUT = 24
ICT_BULUT_VAT = "4650459971"
SELLER_CODE = "100020"
PRODUCT_Z = 393  # Microsoft 365 Business Basic
TEMPLATE_Z = 162
TAX_ID = 8801
ETTN = "PR-A-ETTN-0001"


# --------------------------------------------------------------------------- in-memory Odoo


class PartnerRepo:
    """res.partner by VAT / id (PartnerMatchingEngine shape) + archived-inclusive proof read."""

    def __init__(self, *partners: Partner) -> None:
        self.partners = {partner.id: partner for partner in partners}
        self.vat_lookups = 0
        self.proof_reads: list[tuple[int, ...]] = []

    def find_by_tax_number(self, tax_number: str, *, company_id: int | None = None):
        self.vat_lookups += 1
        return tuple(p for p in self.partners.values() if p.tax_number == tax_number)

    def find_by_ids(self, ids):
        return tuple(self.partners[i] for i in ids if i in self.partners and self.partners[i].active)

    def find_by_ids_including_archived(self, ids):
        self.proof_reads.append(tuple(ids))
        return tuple(self.partners[i] for i in ids if i in self.partners)


def _odoo_with_product_z() -> InMemoryOdoo:
    odoo = InMemoryOdoo()
    odoo.variants[PRODUCT_Z] = {
        "tmpl": TEMPLATE_Z,
        "name": "Microsoft 365 Business Basic",
        "active": True,
        "company_id": None,
    }
    odoo.variants[394] = {"tmpl": 163, "name": "Microsoft 365 Business Standard", "active": True, "company_id": None}
    return odoo


def _supplierinfo(odoo: InMemoryOdoo, *, partner_id: int, code: str = SELLER_CODE, tmpl: int = TEMPLATE_Z, pid=None):
    odoo.supplierinfo.append(
        {
            "id": 1000 + len(odoo.supplierinfo),
            "partner_id": partner_id,
            "product_tmpl_id": tmpl,
            "product_id": pid,
            "product_code": code,
            "company_id": COMPANY,
        }
    )


class Taxes:
    def map_invoice(self, invoice: InternalInvoice, *, company_id: int | None = None) -> InvoiceTaxMappingResult:
        return InvoiceTaxMappingResult(
            line_results=tuple(
                InvoiceTaxLineResult(
                    line_number=line.line_number,
                    tax_index=idx,
                    result=TaxMatchResult(
                        status=TaxMatchStatus.MATCHED,
                        tax_id=TAX_ID,
                        company_id=COMPANY,
                        tax_type=TaxType.VAT,
                        tax_rate=Decimal("20"),
                        matched_by="company_type_rate",
                        confidence=Decimal("1.00"),
                        reason="matched",
                        candidate_count=1,
                    ),
                )
                for line in invoice.lines
                for idx, _tax in enumerate(line.taxes)
            )
        )


def _engine(partners: PartnerRepo, odoo: InMemoryOdoo, default_codes: dict[str, int] | None = None):
    provider = SimpleNamespace(partner_repository=partners, product_repository=_NoGlobalProducts(default_codes))
    return DecisionEngine(
        rule_engine=DeterministicRuleEngine(
            partner_matcher=PartnerMatchingEngine(provider),
            product_matcher=ProductMatchingEngine(provider, supplier_product_repository=odoo),
            tax_mapper=Taxes(),
        ),
        strategy_resolver=WorkflowStrategyResolver([VendorBillReviewRecommendationStrategy(), ManualReviewStrategy()]),
    )


def _invoice(*, vat: str = VAT, codes: tuple[str | None, ...] = (SELLER_CODE,)) -> InternalInvoice:
    lines = tuple(
        InvoiceLine(
            line_number=str(index),
            description="Microsoft 365 Business Basic",
            seller_item_code=code,
            quantity=Decimal("1"),
            unit_code="C62",
            unit_price=Decimal("100"),
            line_extension_amount=Decimal("100"),
            taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
        )
        for index, code in enumerate(codes, start=1)
    )
    total = Decimal(100 * len(lines))
    return InternalInvoice(
        header=Header(
            invoice_number="ICF2026000009001",
            invoice_uuid=ETTN,
            ettn=ETTN,
            issue_date=date(2026, 10, 1),
            currency_code="TRY",
        ),
        supplier=Party(name="SUPPLIER", tax_number=vat),
        customer=Party(name="ICT", tax_number="4651205941"),
        totals=MonetaryTotals(
            line_extension_amount=total,
            tax_exclusive_amount=total,
            tax_inclusive_amount=total * Decimal("1.2"),
            payable_amount=total * Decimal("1.2"),
        ),
        lines=lines,
    )


class EffectReader:
    def __init__(self, effect: SupplierRemediationEffect | None = None) -> None:
        self.effect = effect
        self.calls = 0

    def find_latest_remediation_effect(self, *, review_id: str, company_id: int):
        self.calls += 1
        return self.effect


def _effect(review_id: str, *, partner_id: int = PARTNER_X, mode=SupplierResolutionMode.MATCH_EXISTING):
    status = (
        SupplierPartnerWriteEffectStatus.SELECTED
        if mode is SupplierResolutionMode.MATCH_EXISTING
        else SupplierPartnerWriteEffectStatus.ALREADY_EXISTS
    )
    return SupplierRemediationEffect(
        review_id=review_id,
        company_id=COMPANY,
        review_version=1,
        source_invoice_id=ETTN,
        mode=mode,
        resolved_partner_id=partner_id,
        partner_write_status=status,
    )


# --------------------------------------------------------------------------- SQLite review harness


@pytest.fixture()
def session() -> Session:
    from app.db.base import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewExecutionEvidence.__table__,
            WorkbenchReviewClassificationEvidence.__table__,
            WorkbenchReviewSourceInvoiceEvidence.__table__,
            WorkbenchReviewSourceInvoiceCorrection.__table__,
            WorkbenchReviewReclassification.__table__,
        ],
    )
    with sessionmaker(bind=engine)() as db_session:
        yield db_session


class _NoHistory:
    def find_imported_invoice(self, idempotency_key: str) -> None:
        return None

    def record_import_result(self, **kwargs: Any) -> None:
        return None


async def _import(session: Session, engine: DecisionEngine, invoice: InternalInvoice) -> str:
    result = await ImportInvoiceUseCase(
        import_history=_NoHistory(),
        decision_engine=engine,
        review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(session)),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    ).execute(ImportInvoiceCommand(invoice=invoice, idempotency_key=f"uyumsoft:1:{ETTN}", company_id=COMPANY))
    assert result.review_id is not None
    return result.review_id


def _reclassifier(session: Session, engine: DecisionEngine, effects: EffectReader, partners) -> Any:
    return ReclassifyWorkbenchReviewUseCase(
        decision_engine=engine,
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        reclassification_writer=SqlAlchemyReviewRepository(session),
        supplier_remediation_effect_reader=effects,
        supplier_partner_reader=partners,
    )


async def _reclassify(session: Session, reclassifier: Any, review_id: str, version: int):
    outcome = await reclassifier.execute(
        ReclassifyReviewCommand(
            review_id=review_id,
            company_id=COMPANY,
            expected_version=version,
            trigger=ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
        )
    )
    session.commit()
    return outcome


def _codes(reasons) -> set[tuple[ManualReviewReasonCode, str | None]]:
    return {(reason.code, reason.line_number) for reason in reasons}


def _stage1_line(session: Session, review_id: str, version: int):
    evidence = SqlAlchemyReviewRepository(session).get_review_execution_evidence(
        review_id=review_id, company_id=COMPANY, review_version=version
    )
    return evidence, evidence.product_match.line_results[0].result


# =========================================================================== 6: the actual MATCH_EXISTING gap


async def test_match_existing_supplier_resolves_seller_code_through_its_supplierinfo_on_reclassification(
    session: Session,
) -> None:
    """THE regression. Raw: two distinct commercial heads share the VAT (MULTIPLE_MATCHES)
    -- the raw state never names X. Accepted: MATCH_EXISTING -> X. Odoo: supplierinfo
    (X, 100020) -> 393. Reclassification must resolve 100020 -> 393 under X.

    On e58b2c3 this fails: product matching received the raw ambiguous partner match, so
    the line stayed PRODUCT_NOT_FOUND (verified against a main worktree, see PR body)."""

    partners = PartnerRepo(head(PARTNER_X, vat=VAT), head(PARTNER_OTHER, vat=VAT))
    odoo = _odoo_with_product_z()
    engine = _engine(partners, odoo)
    review_id = await _import(session, engine, _invoice())
    item = SqlAlchemyReviewRepository(session).get_review_item(ReviewDetailQuery(review_id=review_id, company_id=1))
    assert _codes(item.review_reasons) == {
        (ManualReviewReasonCode.SUPPLIER_AMBIGUOUS, None),
        (ManualReviewReasonCode.PRODUCT_NOT_FOUND, "1"),
    }

    _supplierinfo(odoo, partner_id=PARTNER_X)
    effects = EffectReader(_effect(review_id))
    outcome = await _reclassify(session, _reclassifier(session, engine, effects, partners), review_id, 1)

    assert outcome.new_review_reasons == ()
    assert outcome.new_workflow is WorkflowType.VENDOR_BILL
    evidence, line = _stage1_line(session, review_id, 2)
    assert (line.status, line.product_id, line.matched_by) == (
        ProductMatchStatus.MATCHED,
        PRODUCT_Z,
        "supplier_product_code",
    )
    # Execution uses the very same supplier product matching used.
    assert (evidence.partner_match.partner_id, evidence.partner_match.matched_by) == (
        PARTNER_X,
        ACCEPTED_SUPPLIER_MATCHED_BY,
    )
    assert partners.proof_reads == [(PARTNER_X,)]  # proven read-only, never written


async def test_same_scenario_without_the_accepted_resolution_stays_product_not_found(session: Session) -> None:
    partners = PartnerRepo(head(PARTNER_X, vat=VAT), head(PARTNER_OTHER, vat=VAT))
    odoo = _odoo_with_product_z()
    engine = _engine(partners, odoo)
    review_id = await _import(session, engine, _invoice())
    _supplierinfo(odoo, partner_id=PARTNER_X)

    outcome = await _reclassify(session, _reclassifier(session, engine, EffectReader(None), partners), review_id, 1)

    assert (ManualReviewReasonCode.PRODUCT_NOT_FOUND, "1") in _codes(outcome.new_review_reasons)
    assert (ManualReviewReasonCode.SUPPLIER_AMBIGUOUS, None) in _codes(outcome.new_review_reasons)


@pytest.mark.parametrize(
    ("proof", "why"),
    [
        (contact(PARTNER_X, parent_id=PARTNER_OTHER, vat=VAT), "contact"),
        (None, "missing"),
        (head(PARTNER_X, company_id=2, vat=VAT), "other company"),
    ],
)
async def test_unproven_accepted_supplier_fails_closed_everywhere(session: Session, proof, why) -> None:
    """C: an accepted resolution whose partner is a contact / gone / foreign is never used --
    not for products, not for execution, and SUPPLIER_AMBIGUOUS is not stripped."""

    partners = PartnerRepo(head(PARTNER_X, vat=VAT), head(PARTNER_OTHER, vat=VAT))
    odoo = _odoo_with_product_z()
    engine = _engine(partners, odoo)
    review_id = await _import(session, engine, _invoice())
    _supplierinfo(odoo, partner_id=PARTNER_X)

    proof_reader = CanonicalPartners({PARTNER_X: proof})
    outcome = await _reclassify(
        session, _reclassifier(session, engine, EffectReader(_effect(review_id)), proof_reader), review_id, 1
    )

    assert _codes(outcome.new_review_reasons) == {
        (ManualReviewReasonCode.SUPPLIER_AMBIGUOUS, None),
        (ManualReviewReasonCode.PRODUCT_NOT_FOUND, "1"),
    }, why
    assert outcome.executable is False


async def test_conflicting_supplierinfo_under_the_effective_supplier_still_fails_closed(session: Session) -> None:
    """D: two (X, 100020) rows naming different templates -> never a guess."""

    partners = PartnerRepo(head(PARTNER_X, vat=VAT), head(PARTNER_OTHER, vat=VAT))
    odoo = _odoo_with_product_z()
    engine = _engine(partners, odoo)
    review_id = await _import(session, engine, _invoice())
    _supplierinfo(odoo, partner_id=PARTNER_X, tmpl=TEMPLATE_Z)
    _supplierinfo(odoo, partner_id=PARTNER_X, tmpl=163)

    outcome = await _reclassify(
        session, _reclassifier(session, engine, EffectReader(_effect(review_id)), partners), review_id, 1
    )

    line_codes = {code for code, line in _codes(outcome.new_review_reasons) if line == "1"}
    assert line_codes and line_codes <= {
        ManualReviewReasonCode.PRODUCT_NOT_FOUND,
        ManualReviewReasonCode.PRODUCT_AMBIGUOUS,
    }
    assert outcome.new_workflow is WorkflowType.MANUAL_REVIEW


async def test_seller_code_equal_to_a_global_default_code_stays_advisory_under_the_effective_supplier(
    session: Session,
) -> None:
    """E (#209): without (X, 100020) supplierinfo, product.default_code == 100020 never matches."""

    partners = PartnerRepo(head(PARTNER_X, vat=VAT), head(PARTNER_OTHER, vat=VAT))
    odoo = _odoo_with_product_z()
    engine = _engine(partners, odoo, default_codes={SELLER_CODE: PRODUCT_Z})
    review_id = await _import(session, engine, _invoice())

    outcome = await _reclassify(
        session, _reclassifier(session, engine, EffectReader(_effect(review_id)), partners), review_id, 1
    )

    assert (ManualReviewReasonCode.PRODUCT_NOT_FOUND, "1") in _codes(outcome.new_review_reasons)


@pytest.mark.parametrize(
    "mode", [SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, SupplierResolutionMode.ONE_OFF_VENDOR]
)
async def test_create_permanent_and_one_off_vendor_effects_are_effective_suppliers_too(
    session: Session, mode: SupplierResolutionMode
) -> None:
    """The created/reused partner is archived here (ONE_OFF_VENDOR archive-last, P0-PROD-10D),
    so the raw matcher cannot see it; the proven effect makes it the effective supplier."""

    partners = PartnerRepo(head(PARTNER_X, vat=VAT, active=False))
    odoo = _odoo_with_product_z()
    engine = _engine(partners, odoo)
    review_id = await _import(session, engine, _invoice())
    _supplierinfo(odoo, partner_id=PARTNER_X)

    outcome = await _reclassify(
        session, _reclassifier(session, engine, EffectReader(_effect(review_id, mode=mode)), partners), review_id, 1
    )

    # SUPPLIER_NOT_FOUND is never stripped (unchanged 10D semantics) -- but the product resolves.
    assert _codes(outcome.new_review_reasons) == {(ManualReviewReasonCode.SUPPLIER_NOT_FOUND, None)}
    _evidence, line = _stage1_line(session, review_id, 2)
    assert (line.status, line.product_id) == (ProductMatchStatus.MATCHED, PRODUCT_Z)


# =========================================================================== A/B: deterministic suppliers


async def test_deterministic_vat_supplier_resolves_without_consulting_any_accepted_resolution(
    session: Session,
) -> None:
    """A + production-proven ICT Bulut shape: partner 24, 100020 -> 393 (supplierinfo 8)."""

    partners = PartnerRepo(head(ICT_BULUT, vat=ICT_BULUT_VAT))
    odoo = _odoo_with_product_z()
    _supplierinfo(odoo, partner_id=ICT_BULUT)
    engine = _engine(partners, odoo)
    review_id = await _import(session, engine, _invoice(vat=ICT_BULUT_VAT, codes=(SELLER_CODE, "100021")))
    effects = EffectReader(_effect(review_id, partner_id=PARTNER_OTHER))

    outcome = await _reclassify(session, _reclassifier(session, engine, effects, partners), review_id, 1)

    assert _codes(outcome.new_review_reasons) == {(ManualReviewReasonCode.PRODUCT_NOT_FOUND, "2")}
    assert effects.calls == 0 and partners.proof_reads == []  # raw match wins; nothing else read
    evidence = SqlAlchemyReviewRepository(session).get_review_execution_evidence(
        review_id=review_id, company_id=COMPANY, review_version=1
    )
    first = evidence.product_match.line_results[0].result
    assert (first.status, first.product_id, evidence.partner_match.partner_id) == (
        ProductMatchStatus.MATCHED,
        PRODUCT_Z,
        ICT_BULUT,
    )


async def test_child_contact_sharing_the_vat_resolves_to_its_commercial_partner(session: Session) -> None:
    """B (#206): head 24 + child 25 with the same VAT -> one counterparty, supplierinfo of 24."""

    partners = PartnerRepo(head(ICT_BULUT, vat=ICT_BULUT_VAT), contact(25, parent_id=ICT_BULUT, vat=ICT_BULUT_VAT))
    odoo = _odoo_with_product_z()
    _supplierinfo(odoo, partner_id=ICT_BULUT)
    _supplierinfo(odoo, partner_id=25, code="ONLY-ON-CHILD", tmpl=163)
    engine = _engine(partners, odoo)

    review_id = await _import(session, engine, _invoice(vat=ICT_BULUT_VAT, codes=(SELLER_CODE, "ONLY-ON-CHILD")))
    item = SqlAlchemyReviewRepository(session).get_review_item(ReviewDetailQuery(review_id=review_id, company_id=1))

    # The commercial partner's mapping applies; a mapping held only by the child contact does not.
    assert _codes(item.review_reasons) == {(ManualReviewReasonCode.PRODUCT_NOT_FOUND, "2")}


# =========================================================================== resolver units


def test_precedence_raw_match_wins_then_proven_accepted_then_nothing() -> None:
    accepted = AcceptedSupplier(origin=EffectiveSupplierOrigin.MATCH_EXISTING, partner_id=PARTNER_X)
    raw = raw_matched(ICT_BULUT)

    deterministic = resolve_effective_supplier(raw, accepted)
    assert (deterministic.partner_id, deterministic.origin) == (ICT_BULUT, EffectiveSupplierOrigin.DETERMINISTIC)
    assert deterministic.partner_match is raw  # byte-identical raw match, never re-synthesized

    via_accepted = resolve_effective_supplier(raw_ambiguous(), accepted)
    assert (via_accepted.partner_id, via_accepted.origin, via_accepted.accepted) == (
        PARTNER_X,
        EffectiveSupplierOrigin.MATCH_EXISTING,
        True,
    )
    assert resolve_effective_supplier(raw_ambiguous(), None) is None
    assert resolve_effective_supplier(None, None) is None
    ambiguous = raw_ambiguous()
    assert supplier_match_for_product_matching(ambiguous, None) is ambiguous  # unchanged input


def test_accepted_supplier_dto_rejects_deterministic_origin_and_bad_ids() -> None:
    with pytest.raises(ValueError):
        AcceptedSupplier(origin=EffectiveSupplierOrigin.DETERMINISTIC, partner_id=1)
    with pytest.raises(ValueError):
        AcceptedSupplier(origin=EffectiveSupplierOrigin.MATCH_EXISTING, partner_id=0)


def test_accepted_supplier_reader_statuses() -> None:
    effect = _effect("r1")
    assert (
        AcceptedSupplierReader(effect_reader=EffectReader(None), partner_reader=CanonicalPartners())
        .lookup(review_id="r1", company_id=COMPANY)
        .status
        is AcceptedSupplierStatus.NONE
    )

    proven = AcceptedSupplierReader(effect_reader=EffectReader(effect), partner_reader=CanonicalPartners()).lookup(
        review_id="r1", company_id=COMPANY
    )
    assert proven.status is AcceptedSupplierStatus.PROVEN and proven.supplier.partner_id == PARTNER_X

    contact_reader = CanonicalPartners({PARTNER_X: contact(PARTNER_X, parent_id=5)})
    unproven = AcceptedSupplierReader(effect_reader=EffectReader(effect), partner_reader=contact_reader).lookup(
        review_id="r1", company_id=COMPANY
    )
    assert unproven.status is AcceptedSupplierStatus.UNPROVEN and unproven.supplier is None
    assert "alt kişisi" in unproven.reason

    broken = CanonicalPartners(error=RuntimeError("odoo down"))
    with pytest.raises(EffectiveSupplierReadError):
        AcceptedSupplierReader(effect_reader=EffectReader(effect), partner_reader=broken).lookup(
            review_id="r1", company_id=COMPANY
        )


def test_resolver_surfaces_matcher_failure_and_skips_effects_on_raw_match() -> None:
    class _Broken:
        def match_invoice(self, invoice, *, company_id=None):
            raise PartnerMatchingError("Partner repository lookup failed.")

    effects = EffectReader(_effect("r1"))
    reader = AcceptedSupplierReader(effect_reader=effects, partner_reader=CanonicalPartners())
    with pytest.raises(EffectiveSupplierReadError):
        EffectiveSupplierResolver(partner_matcher=_Broken(), accepted_supplier_reader=reader).resolve(
            review_id="r1", company_id=COMPANY, invoice=_invoice()
        )

    resolution = EffectiveSupplierResolver(
        partner_matcher=FixedPartnerMatcher(raw_matched(ICT_BULUT)), accepted_supplier_reader=reader
    ).resolve(review_id="r1", company_id=COMPANY, invoice=_invoice())
    assert resolution.supplier.partner_id == ICT_BULUT and effects.calls == 0

    unresolved = EffectiveSupplierResolver(
        partner_matcher=FixedPartnerMatcher(raw_not_found()),
        accepted_supplier_reader=AcceptedSupplierReader(effect_reader=EffectReader(None), partner_reader=None),  # type: ignore[arg-type]
    ).resolve(review_id="r1", company_id=COMPANY, invoice=_invoice())
    assert unresolved.supplier is None and unresolved.failure


def test_effect_reader_without_partner_reader_is_a_wiring_error() -> None:
    with pytest.raises(WorkbenchContractError):
        EffectiveDecisionResolver(
            decision_engine=object(),  # type: ignore[arg-type]
            source_invoice_reader=object(),  # type: ignore[arg-type]
            supplier_remediation_effect_reader=EffectReader(None),  # type: ignore[arg-type]
        )


def test_odoo_proof_read_includes_archived_partners_and_is_read_only() -> None:
    class _Adapter:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def search_read_all(self, **kwargs: Any):
            self.calls.append(kwargs)
            return [{"id": 452, "name": "Pelit", "vat": "1", "active": False, "commercial_partner_id": [452, "P"]}]

    adapter = _Adapter()
    (partner,) = OdooPartnerRepository(adapter=adapter).find_by_ids_including_archived((452,))  # type: ignore[arg-type]
    assert adapter.calls[0]["domain"] == [["id", "in", [452]], ["active", "in", [True, False]]]
    assert adapter.calls[0]["model"] == "res.partner"
    assert (partner.id, partner.active, partner.commercial_partner_id) == (452, False, 452)


# =========================================================================== 4/F: MapExisting end to end


class _NoClaim:
    def find(self, **kwargs: Any) -> None:
        return None


def _map_use_case(session: Session, odoo: InMemoryOdoo, partners: PartnerRepo, effects: EffectReader, engine):
    reader = AcceptedSupplierReader(effect_reader=effects, partner_reader=partners)
    provider = SimpleNamespace(partner_repository=partners)
    review_repository = SqlAlchemyReviewRepository(session)
    return MapExistingProductUseCase(
        review_reader=review_repository,
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        effective_supplier_resolver=EffectiveSupplierResolver(
            partner_matcher=PartnerMatchingEngine(provider), accepted_supplier_reader=reader
        ),
        product_reader=odoo,
        identity_claim_reader=_NoClaim(),
        existing_supplier_info_reader=OdooExistingSupplierInfoReader(
            repository=OdooSupplierInfoRepository(client=odoo)
        ),
        supplier_info_writer=OdooSupplierInfoWriter(
            repository=OdooSupplierInfoRepository(client=odoo),
            # Test-only enabled policy against a staging host; production stays default-off.
            policy=OdooProductWritePolicy(
                product_remediation_write_enabled=True, app_env="staging", odoo_host="test-ictteknoloji.odoo.com"
            ),
        ),
        reclassifier=_reclassifier(session, engine, effects, partners),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


def _map_command(review_id: str, version: int) -> MapExistingProductCommand:
    return MapExistingProductCommand(
        review_id=review_id,
        company_id=COMPANY,
        expected_version=version,
        line_number="1",
        product_id=PRODUCT_Z,
        approved_by="onur",
    )


async def test_map_existing_under_match_existing_writes_for_x_and_the_line_resolves(session: Session) -> None:
    partners = PartnerRepo(head(PARTNER_X, vat=VAT), head(PARTNER_OTHER, vat=VAT))
    odoo = _odoo_with_product_z()
    engine = _engine(partners, odoo)
    invoice = _invoice()
    review_id = await _import(session, engine, invoice)
    effects = EffectReader(_effect(review_id))
    # SUPPLIER_RESOLUTION reclassification after MATCH_EXISTING (v1 -> v2): only PRODUCT_NOT_FOUND left.
    v2 = await _reclassify(session, _reclassifier(session, engine, effects, partners), review_id, 1)
    assert _codes(v2.new_review_reasons) == {(ManualReviewReasonCode.PRODUCT_NOT_FOUND, "1")}

    result = await _map_use_case(session, odoo, partners, effects, engine).execute(_map_command(review_id, 2))

    assert [row["partner_id"] for row in odoo.create_calls] == [PARTNER_X]  # written for X ...
    assert result.supplier_partner_id == PARTNER_X
    assert result.line_resolved is True and result.remaining_product_lines == ()  # ... and found again for X
    assert result.current_version == 3


async def test_map_existing_ict_bulut_deterministic_path_is_unchanged(session: Session) -> None:
    """F: production-proven shape -- partner 24, 100020 -> 393 -- with line 2 still open."""

    partners = PartnerRepo(head(ICT_BULUT, vat=ICT_BULUT_VAT))
    odoo = _odoo_with_product_z()
    engine = _engine(partners, odoo)
    review_id = await _import(session, engine, _invoice(vat=ICT_BULUT_VAT, codes=(SELLER_CODE, "100021")))
    effects = EffectReader(None)

    result = await _map_use_case(session, odoo, partners, effects, engine).execute(_map_command(review_id, 1))

    assert odoo.create_calls == [
        {
            "partner_id": ICT_BULUT,
            "product_tmpl_id": TEMPLATE_Z,
            "product_code": SELLER_CODE,
            "company_id": COMPANY,
            "product_id": PRODUCT_Z,
            "product_name": "Microsoft 365 Business Basic",
        }
    ]
    assert (result.line_resolved, result.remaining_product_lines) == (True, ("2",))
    assert effects.calls == 0 and partners.proof_reads == []


async def test_map_existing_with_an_unproven_accepted_supplier_writes_nothing(session: Session) -> None:
    partners = PartnerRepo(head(PARTNER_X, vat=VAT), head(PARTNER_OTHER, vat=VAT))
    odoo = _odoo_with_product_z()
    engine = _engine(partners, odoo)
    review_id = await _import(session, engine, _invoice())
    effects = EffectReader(_effect(review_id, partner_id=999))  # partner 999 does not exist in Odoo

    with pytest.raises(ProductRemediationSupplierUnresolvedError) as caught:
        await _map_use_case(session, odoo, partners, effects, engine).execute(_map_command(review_id, 1))

    assert odoo.create_calls == []
    assert "bulunamadı" in caught.value.safe_message


# =========================================================================== 7A: parent request inputs


def _product_mapping_field_mapping() -> OdooOperatorRequestFieldMapping:
    base = _request_mapping()
    values = {name: getattr(base, name) for name in base.__dataclass_fields__}
    values.update(line="x_studio_ipp_req_line", product="x_studio_ipp_req_product")
    return OdooOperatorRequestFieldMapping(**values)


def test_odoo_acknowledger_clears_action_line_and_product_only_when_asked() -> None:
    processed = datetime(2026, 10, 8, 9, 1, tzinfo=UTC)
    adapter = FakeJson2([_row()])
    ack = OdooOperatorRequestAcknowledger(adapter=adapter, mapping=_product_mapping_field_mapping())

    assert ack.acknowledge(
        odoo_record_id=28,
        requested_at=REQUESTED_AT,
        outcome=OperatorRequestOutcome.COMPLETED,
        message="Satır 1 eşleştirildi.",
        processed_at=processed,
        clear_request_inputs=True,
    )
    (_record, values) = adapter.writes[0]
    assert values == {
        "x_studio_ipp_req_result": values["x_studio_ipp_req_result"],
        "x_studio_ipp_req_message": "Satır 1 eşleştirildi.",
        "x_studio_ipp_req_processed_at": "2026-10-08 09:01:00",
        "x_studio_ipp_req_ready": False,
        "x_studio_ipp_req_action": False,
        "x_studio_ipp_req_line": False,
        "x_studio_ipp_req_product": False,
    }
    assert values["x_studio_ipp_req_result"]  # result is written, never cleared

    untouched = FakeJson2([_row()])
    OdooOperatorRequestAcknowledger(adapter=untouched, mapping=_product_mapping_field_mapping()).acknowledge(
        odoo_record_id=28,
        requested_at=REQUESTED_AT,
        outcome=OperatorRequestOutcome.REJECTED,
        message="x",
        processed_at=processed,
    )
    assert not {"x_studio_ipp_req_action", "x_studio_ipp_req_line", "x_studio_ipp_req_product"} & set(
        untouched.writes[0][1]
    )


def _product_mapping_request():
    return _request(
        action=OperatorRequestAction.PRODUCT_MAPPING, purchase_purpose=None, line_number="1", product_id=PRODUCT_Z
    )


@pytest.mark.parametrize(
    ("outcome", "clears"),
    [
        (OperatorRequestOutcome.COMPLETED, True),
        (OperatorRequestOutcome.ALREADY_COMPLETED, True),
        (OperatorRequestOutcome.REJECTED, False),
        (OperatorRequestOutcome.FAILED, False),
        (OperatorRequestOutcome.STALE, False),
    ],
)
def test_ingestion_clears_inputs_only_after_a_successful_product_mapping(outcome, clears) -> None:
    ack = FakeAcknowledger()
    handler = ScriptedHandler([OperatorActionOutcome(outcome=outcome, message="m")])
    _workflow(
        FakeReader([_product_mapping_request()]),
        handler,
        acknowledger=ack,
        action=OperatorRequestAction.PRODUCT_MAPPING,
    ).run(company_id=COMPANY)

    assert ack.calls[-1]["clear_request_inputs"] is clears


def test_other_actions_never_clear_inputs_even_when_completed() -> None:
    ack = FakeAcknowledger()
    handler = ScriptedHandler([OperatorActionOutcome(outcome=OperatorRequestOutcome.COMPLETED, message="ok")])
    _workflow(FakeReader([_request()]), handler, acknowledger=ack).run(company_id=COMPANY)

    assert ack.calls[-1]["clear_request_inputs"] is False


# =========================================================================== 7B: Tamamlananlar


def _guidance_input(reason_codes=()) -> GuidanceInput:
    return GuidanceInput(
        status=ReviewStatus.PENDING_REVIEW,
        reason_codes=tuple(reason_codes),
        supplier_name="ICT BULUT BİLİŞİM A.Ş.",
        decision_type=None,
        decision_workflow=None,
        decision_version=None,
        execution_state=None,
        vendor_bill_id=None,
    )


def test_completed_section_lists_currently_resolved_product_lines_without_attribution() -> None:
    facts = OperatorGuidanceFacts(
        resolved_product_lines=(
            ResolvedProductLine("1", SELLER_CODE, "Microsoft 365 Business Basic", "Microsoft 365 Business Basic"),
            ResolvedProductLine("3", None, None, None),
        )
    )

    html = build_operator_guidance(_guidance_input((ManualReviewReasonCode.PRODUCT_NOT_FOUND,)), facts).completed_html

    assert "✓ Satır 1 — 100020 — Microsoft 365 Business Basic → Microsoft 365 Business Basic" in html
    assert f"✓ Satır 3 → {PRODUCT_LABEL_UNAVAILABLE}" in html
    assert "operatör" not in html.lower() and "eşleştirdi" not in html


class _Evidence:
    def __init__(self, product_match=None, *, missing: bool = False) -> None:
        self.product_match = product_match
        self.missing = missing

    def get_review_execution_evidence(self, *, review_id, company_id, review_version):
        if self.missing:
            raise ReviewNotFoundError("missing")
        assert review_version == 3  # always the review's *current* version
        return SimpleNamespace(product_match=self.product_match)


class _Source:
    def __init__(self, invoice: InternalInvoice) -> None:
        self.invoice = invoice

    def get(self, *, review_id, company_id):
        return SimpleNamespace(invoice=self.invoice)


def _review(*unresolved_lines: str) -> ReviewItem:
    from app.application.workflow import ManualReviewReason

    return ReviewItem(
        review_id="review:dfccd66e",
        invoice_id=ETTN,
        invoice_number="ICF2026000009001",
        supplier_tax_number=ICT_BULUT_VAT,
        supplier_name="ICT BULUT BİLİŞİM A.Ş.",
        invoice_date=date(2026, 10, 1),
        currency="TRY",
        total_amount=Decimal("240"),
        workflow=WorkflowType.MANUAL_REVIEW,
        status=ReviewStatus.PENDING_REVIEW,
        review_reasons=tuple(
            ManualReviewReason(code=ManualReviewReasonCode.PRODUCT_NOT_FOUND, message="x", line_number=n)
            for n in unresolved_lines
        ),
        version=3,
    )


def test_resolved_lines_come_from_current_execution_evidence_and_degrade_safely() -> None:
    from app.composition.imports import _resolved_product_lines

    odoo = _odoo_with_product_z()
    _supplierinfo(odoo, partner_id=ICT_BULUT)
    invoice = _invoice(vat=ICT_BULUT_VAT, codes=(SELLER_CODE, "100021"))
    match = ProductMatchingEngine(
        SimpleNamespace(product_repository=_NoGlobalProducts()), supplier_product_repository=odoo
    ).match_invoice(invoice, company_id=COMPANY, partner_match=raw_matched(ICT_BULUT))
    review = _review("2")

    lines = _resolved_product_lines(review, COMPANY, _Source(invoice), _Evidence(match), odoo)
    assert lines == (
        ResolvedProductLine("1", SELLER_CODE, "Microsoft 365 Business Basic", "Microsoft 365 Business Basic"),
    )

    # No evidence for the current version -> nothing is claimed.
    assert _resolved_product_lines(review, COMPANY, _Source(invoice), _Evidence(missing=True), odoo) == ()

    class _NamesDown:
        def find_products_by_ids(self, ids):
            raise EffectiveSupplierReadError("Odoo down")

    (degraded,) = _resolved_product_lines(review, COMPANY, _Source(invoice), _Evidence(match), _NamesDown())
    assert degraded.product_name is None
