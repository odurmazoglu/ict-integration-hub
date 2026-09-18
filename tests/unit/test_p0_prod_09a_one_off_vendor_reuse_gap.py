"""P0-PROD-09A discovered a real gap: a later invoice from a VAT that already went
through the ONE_OFF_VENDOR archive-last lifecycle once could not be resolved at all.

P0-PROD-09C fixes it. This file, exercised through the REAL Odoo writer (not a fake
that abstracts the active-flag check away), now proves the FIXED behavior: an
archived exact-VAT partner IS reused when -- and only when -- Hub persistence proves
it was previously created via ONE_OFF_VENDOR for this exact partner id.

History: ``test_f_one_off_vendor_reuses_existing_hub_owned_archived_partner`` in
``test_supplier_remediation_orchestration.py`` always asserted this reuse succeeds,
but it was wired against ``_FakeSupplierPartnerWriter`` -- a fake that unconditionally
returns ``ALREADY_EXISTS`` for any VAT match, regardless of the ``active`` flag. It
never modeled ``OdooSupplierPartnerWriter._already_exists_result``'s real behaviour
(``app/erp/write/odoo_supplier_partner_writer.py``), which -- before P0-PROD-09C --
raised ``SupplierPartnerInactiveError`` for *any* inactive exact-VAT match, Hub-owned
or not, before ``_create_or_reuse_one_off_vendor_partner``'s own ownership check ever
ran. This test (originally added in P0-PROD-09A) proved that gap empirically; it is
kept and updated here rather than deleted, since it is still the only real-writer-backed
reconstruction of the exact D-Market shape.

This module makes zero real Odoo writes and zero production calls -- it is a pure
in-memory SQLite unit test using the real application/erp-write classes, with a fake
JSON-2 client standing in for Odoo's HTTP boundary only.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.exceptions.supplier_partner import SupplierPartnerInactiveError
from app.application.workbench.dto import ReviewItem, ReviewStatus
from app.application.workbench.evidence import ReviewSourceInvoiceEvidence
from app.application.workbench.one_off_vendor_retirement import OneOffVendorRetirementStatus
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.reclassification import ReviewReclassificationResult
from app.application.workbench.supplier_remediation import (
    ResolveWorkbenchSupplierCommand,
    SupplierPartnerWriteEffectStatus,
    SupplierRemediationEffect,
    SupplierRemediationStatus,
)
from app.application.workbench.supplier_remediation_use_cases import ResolveWorkbenchSupplierUseCase
from app.application.workbench.supplier_resolution import ResolutionPartnerRecord, SupplierResolutionMode
from app.application.workbench.supplier_resolution_use_cases import ValidateSupplierResolutionUseCase
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.db.base import Base
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.erp.write.odoo_supplier_partner_writer import (
    OdooSupplierPartnerRepository,
    OdooSupplierPartnerWritePolicy,
    OdooSupplierPartnerWriter,
)
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_one_off_vendor_retirement import WorkbenchReviewOneOffVendorRetirement
from app.models.workbench_review_supplier_remediation_effect import WorkbenchReviewSupplierRemediationEffect
from app.persistence import (
    SqlAlchemyReviewOneOffVendorRetirementRepository,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyReviewSupplierResolutionRepository,
    SqlAlchemyUnitOfWork,
)

COMPANY_ID = 1
VKN = "2650179910"  # D-MARKET's real VAT, reused deliberately for this reconstruction
PRIOR_REVIEW_ID = "review:prior-one-off-archived"
NEW_REVIEW_ID = "review:new-invoice-same-vat"
ARCHIVED_PARTNER_ID = 448
ACTOR = "finance.operator"


class _FakeOdooJson2ClientReturningArchivedPartner:
    """Models exactly what production Odoo returns for D-Market's exact-VAT lookup
    the day after partner 448 was archived: one record, ``active: False``."""

    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []

    async def create_res_partner(self, payload: dict[str, Any]) -> int:
        self.create_calls.append(payload)
        raise AssertionError("create_res_partner must never be called: an exact-VAT match already exists.")

    async def search_read(self, *, model: str, domain, fields, limit: int = 20, offset: int = 0):
        self.search_calls.append({"model": model, "domain": domain, "fields": fields, "limit": limit})
        return [
            {
                "id": ARCHIVED_PARTNER_ID,
                "name": "D-MARKET ELEKTRONİK HİZMETLER VE TİCARET ANONİM ŞİRKETİ",
                "vat": VKN,
                "active": False,
                "company_id": [COMPANY_ID, "ICT Teknoloji"],
            }
        ]


def _real_writer() -> tuple[OdooSupplierPartnerWriter, _FakeOdooJson2ClientReturningArchivedPartner]:
    client = _FakeOdooJson2ClientReturningArchivedPartner()
    policy = OdooSupplierPartnerWritePolicy(
        supplier_remediation_write_enabled=True,
        app_env="staging",
        odoo_host="test-ictteknoloji.odoo.com",
    )
    writer = OdooSupplierPartnerWriter(repository=OdooSupplierPartnerRepository(client=client), policy=policy)
    return writer, client


def _source_invoice() -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="HD-NEXT-1",
            invoice_uuid="00000000-0000-4000-8000-0000000009a1",
            ettn="NEXT-ETTN-1",
            issue_date=date(2026, 9, 20),
            currency_code="TRY",
        ),
        supplier=Party(name="D-MARKET ELEKTRONİK HİZMETLER VE TİCARET ANONİM ŞİRKETİ", tax_number=VKN),
        customer=Party(name="ICT TEKNOLOJI", tax_number="1112223334"),
        totals=MonetaryTotals(payable_amount=Decimal("100.00")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Some other product",
                quantity=Decimal("1"),
                unit_code="C62",
                unit_price=Decimal("83.33"),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
            ),
        ),
    )


class _FakeReviewReader:
    """Identical contract to test_supplier_remediation_orchestration.py's own fake."""

    def __init__(self, item: ReviewItem) -> None:
        self.item = item

    def get_review_item(self, query: ReviewDetailQuery) -> ReviewItem:
        return self.item


class _FakeSourceReader:
    def __init__(self, evidence: ReviewSourceInvoiceEvidence) -> None:
        self.evidence = evidence

    def get(self, *, review_id: str, company_id: int) -> ReviewSourceInvoiceEvidence:
        return self.evidence


class _FakePartnerReader:
    def __init__(self, partner: ResolutionPartnerRecord | None) -> None:
        self.partner = partner

    def find_partner_by_id(self, partner_id: int) -> ResolutionPartnerRecord | None:
        return self.partner


class _FakeReclassifier:
    def __init__(self, *, new_reasons: tuple[ManualReviewReason, ...] = ()) -> None:
        self._new_reasons = new_reasons

    async def execute(self, command):
        supplier_found = not any(r.code is ManualReviewReasonCode.SUPPLIER_NOT_FOUND for r in self._new_reasons)
        return ReviewReclassificationResult(
            review_id=command.review_id,
            company_id=command.company_id,
            from_version=command.expected_version,
            to_version=command.expected_version + 1,
            changed=True,
            previous_workflow=WorkflowType.MANUAL_REVIEW,
            new_workflow=WorkflowType.MANUAL_REVIEW,
            previous_review_reasons=(),
            new_review_reasons=self._new_reasons,
            trigger=command.trigger,
            executable=supplier_found,
        )


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            __import__(
                "app.models.workbench_review_supplier_resolution", fromlist=["WorkbenchReviewSupplierResolution"]
            ).WorkbenchReviewSupplierResolution.__table__,
            WorkbenchReviewSupplierRemediationEffect.__table__,
            WorkbenchReviewOneOffVendorRetirement.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        db_session.add(
            WorkbenchReviewItem(
                review_id=PRIOR_REVIEW_ID,
                company_id=COMPANY_ID,
                invoice_id="prior-ettn",
                invoice_number="HD12026000964604",
                supplier_tax_number=VKN,
                supplier_name="D-MARKET ELEKTRONİK HİZMETLER VE TİCARET ANONİM ŞİRKETİ",
                invoice_date=date(2026, 9, 10),
                currency="TRY",
                total_amount=Decimal("676.21"),
                workflow="vendor_bill",
                status="decision_submitted",
                review_reasons=[],
                warnings=[],
                version=4,
                idempotency_key="uyumsoft:1:prior-ettn",
            )
        )
        db_session.add(
            WorkbenchReviewItem(
                review_id=NEW_REVIEW_ID,
                company_id=COMPANY_ID,
                invoice_id="next-ettn",
                invoice_number="HD-NEXT-1",
                supplier_tax_number=VKN,
                supplier_name="D-MARKET ELEKTRONİK HİZMETLER VE TİCARET ANONİM ŞİRKETİ",
                invoice_date=date(2026, 9, 20),
                currency="TRY",
                total_amount=Decimal("100.00"),
                workflow="manual_review",
                status="pending_review",
                review_reasons=[{"code": "supplier_not_found", "message": "x"}],
                warnings=[],
                version=1,
                idempotency_key="uyumsoft:1:next-ettn",
            )
        )
        db_session.flush()
        yield db_session


def _seed_prior_one_off_vendor_effect(session: Session) -> SqlAlchemyReviewSupplierRemediationEffectRepository:
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    effect_repo.create_remediation_effect(
        SupplierRemediationEffect(
            review_id=PRIOR_REVIEW_ID,
            company_id=COMPANY_ID,
            review_version=3,
            source_invoice_id="prior-ettn",
            mode=SupplierResolutionMode.ONE_OFF_VENDOR,
            resolved_partner_id=ARCHIVED_PARTNER_ID,
            partner_write_status=SupplierPartnerWriteEffectStatus.CREATED,
            source_supplier_tax_number=VKN,
            approved_by=ACTOR,
        )
    )
    session.commit()
    # Sanity check: the Hub really does consider partner 448 its own, exactly the
    # condition _create_or_reuse_one_off_vendor_partner's ownership check looks for.
    assert (
        effect_repo.find_one_off_vendor_effect_by_partner_id(
            company_id=COMPANY_ID, resolved_partner_id=ARCHIVED_PARTNER_ID
        )
        is not None
    )
    return effect_repo


def _new_review_use_case(
    session: Session,
    *,
    effect_repo: SqlAlchemyReviewSupplierRemediationEffectRepository,
    writer: OdooSupplierPartnerWriter,
    new_reasons: tuple[ManualReviewReason, ...] = (),
) -> ResolveWorkbenchSupplierUseCase:
    review = ReviewItem(
        review_id=NEW_REVIEW_ID,
        invoice_id="next-ettn",
        invoice_number="HD-NEXT-1",
        supplier_tax_number=VKN,
        supplier_name="D-MARKET ELEKTRONİK HİZMETLER VE TİCARET ANONİM ŞİRKETİ",
        invoice_date=date(2026, 9, 20),
        currency="TRY",
        total_amount=Decimal("100.00"),
        workflow=WorkflowType.MANUAL_REVIEW,
        status=ReviewStatus.PENDING_REVIEW,
        review_reasons=(
            ManualReviewReason(
                code=ManualReviewReasonCode.SUPPLIER_NOT_FOUND,
                message="No active supplier partner for VKN.",
                source="partner_matching",
                candidate_count=0,
            ),
        ),
        version=1,
    )
    source = ReviewSourceInvoiceEvidence(
        review_id=NEW_REVIEW_ID,
        company_id=COMPANY_ID,
        review_version=1,
        source_invoice_id="NEXT-ETTN-1",
        invoice=_source_invoice(),
    )
    return ResolveWorkbenchSupplierUseCase(
        review_reader=_FakeReviewReader(review),
        source_invoice_reader=_FakeSourceReader(source),
        resolution_validator=ValidateSupplierResolutionUseCase(
            source_invoice_reader=_FakeSourceReader(source),
            partner_reader=_FakePartnerReader(None),
        ),
        resolution_writer=SqlAlchemyReviewSupplierResolutionRepository(session),
        remediation_effect_writer=effect_repo,
        supplier_partner_writer=writer,
        reclassifier=_FakeReclassifier(new_reasons=new_reasons),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        workbench_republisher=None,
        retirement_writer=SqlAlchemyReviewOneOffVendorRetirementRepository(session),
    )


def _command() -> ResolveWorkbenchSupplierCommand:
    return ResolveWorkbenchSupplierCommand(
        review_id=NEW_REVIEW_ID,
        company_id=COMPANY_ID,
        expected_version=1,
        mode=SupplierResolutionMode.ONE_OFF_VENDOR,
        approved_by=ACTOR,
        resolved_partner_id=None,
    )


async def test_next_invoice_same_vat_as_archived_hub_owned_partner_is_reused_not_blocked(
    session: Session,
) -> None:
    """P0-PROD-09C fix: the exact D-Market-shaped repeat-VAT scenario, through the REAL
    writer. A brand-new review, same company, same VAT, resolved with
    ``SupplierResolutionMode.ONE_OFF_VENDOR`` now reuses the archived, Hub-owned
    partner 448 -- no duplicate partner, no reactivation write, a fresh
    review-specific SupplierRemediationEffect and PENDING_VENDOR_BILL retirement row.
    """

    effect_repo = _seed_prior_one_off_vendor_effect(session)
    writer, client = _real_writer()
    use_case = _new_review_use_case(session, effect_repo=effect_repo, writer=writer)

    result = await use_case.execute(_command())

    assert result.status is SupplierRemediationStatus.RESOLVED
    assert result.effective_partner_id == ARCHIVED_PARTNER_ID
    assert result.partner_write_status is SupplierPartnerWriteEffectStatus.ALREADY_EXISTS
    assert result.one_off_vendor_hub_owned is True
    assert result.one_off_vendor_retirement_status is OneOffVendorRetirementStatus.PENDING_VENDOR_BILL

    # No duplicate partner was ever attempted.
    assert client.create_calls == []

    # A fresh, review-specific effect and retirement row exist for the NEW review,
    # both pointing at the SAME reused partner id -- the prior review's own rows are
    # untouched.
    new_effect = effect_repo.find_remediation_effect(review_id=NEW_REVIEW_ID, company_id=COMPANY_ID, review_version=1)
    assert new_effect is not None
    assert new_effect.resolved_partner_id == ARCHIVED_PARTNER_ID
    assert new_effect.mode is SupplierResolutionMode.ONE_OFF_VENDOR

    retirement_repo = SqlAlchemyReviewOneOffVendorRetirementRepository(session)
    new_retirement = retirement_repo.find(review_id=NEW_REVIEW_ID, company_id=COMPANY_ID, review_version=1)
    assert new_retirement is not None
    assert new_retirement.resolved_partner_id == ARCHIVED_PARTNER_ID
    assert new_retirement.status is OneOffVendorRetirementStatus.PENDING_VENDOR_BILL

    prior_retirement_count = (
        session.query(WorkbenchReviewOneOffVendorRetirement).filter_by(review_id=PRIOR_REVIEW_ID).count()
    )
    assert prior_retirement_count == 0  # the prior review never had its own retirement row in this fixture

    assert session.query(WorkbenchReviewSupplierRemediationEffect).filter_by(review_id=NEW_REVIEW_ID).count() == 1


async def test_inactive_non_hub_owned_exact_vat_still_fails_closed_after_the_fix(session: Session) -> None:
    """Case B is unaffected by the P0-PROD-09C fix: an inactive exact-VAT match with NO
    prior ONE_OFF_VENDOR effect still fails closed -- the fix only widens reuse to the
    proven-owned case, never to an arbitrary archived partner."""

    # No prior effect is seeded at all -- the Hub has never heard of partner 448.
    effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
    writer, client = _real_writer()
    use_case = _new_review_use_case(session, effect_repo=effect_repo, writer=writer)

    # No prior effect for this partner id -> the writer's own default fail-closed
    # behavior fires (SupplierPartnerInactiveError), exactly as before the fix. The
    # orchestration-level SupplierResolutionOneOffVendorNotHubOwnedError is reserved
    # for the ACTIVE-but-not-owned case (see test_h_* in
    # test_supplier_remediation_orchestration.py) -- the writer never even returns
    # ALREADY_EXISTS here for that check to run against.
    with pytest.raises(SupplierPartnerInactiveError):
        await use_case.execute(_command())

    assert client.create_calls == []
    assert session.query(WorkbenchReviewSupplierRemediationEffect).filter_by(review_id=NEW_REVIEW_ID).count() == 0
    assert session.query(WorkbenchReviewOneOffVendorRetirement).filter_by(review_id=NEW_REVIEW_ID).count() == 0
