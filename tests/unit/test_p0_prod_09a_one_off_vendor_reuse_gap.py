"""P0-PROD-09A investigation: does the Hub reuse an archived Hub-owned ONE_OFF_VENDOR
partner for a later invoice from the same VAT, exercised through the REAL Odoo writer?

Context: ``test_f_one_off_vendor_reuses_existing_hub_owned_archived_partner`` in
``test_supplier_remediation_orchestration.py`` asserts this reuse succeeds, but it is
wired against ``_FakeSupplierPartnerWriter`` -- a fake that unconditionally returns
``ALREADY_EXISTS`` for any VAT match, regardless of the ``active`` flag. It never models
``OdooSupplierPartnerWriter._already_exists_result``'s real behaviour
(``app/erp/write/odoo_supplier_partner_writer.py``), which raises
``SupplierPartnerInactiveError`` for *any* inactive exact-VAT match -- Hub-owned or not --
before ``_create_or_reuse_one_off_vendor_partner``'s own ownership check
(``app/application/workbench/supplier_remediation_use_cases.py:411-420``) ever runs.

This test exercises the exact same orchestration path through the REAL
``OdooSupplierPartnerWriter`` (not the fake) to prove, empirically, what actually
happens for a real archived Hub-owned partner: it fails closed with
``SupplierPartnerInactiveError``, not a silent reuse. The comment at
``supplier_remediation_use_cases.py:307-314`` documents the *intended* behaviour
("reusing a partner this Hub already archived ... is a legitimate, expected state, not
an error") -- this test proves that intent is currently unreachable in production,
exactly the same shape of gap as P0-PROD-08M's execution-evidence gate. See the
P0-PROD-09A gap register (docs/) for the proposed follow-up.

This module makes zero Odoo writes and zero production calls -- it is a pure in-memory
SQLite unit test using the real application/erp-write classes.
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
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.supplier_remediation import (
    ResolveWorkbenchSupplierCommand,
    SupplierPartnerWriteEffectStatus,
    SupplierRemediationEffect,
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

    async def create_res_partner(self, payload: dict[str, Any]) -> int:
        self.create_calls.append(payload)
        raise AssertionError("create_res_partner must never be called: an exact-VAT match already exists.")

    async def search_read(self, *, model: str, domain, fields, limit: int = 20, offset: int = 0):
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
    async def execute(self, command):
        from app.application.workbench.reclassification import ReviewReclassificationResult

        return ReviewReclassificationResult(
            review_id=command.review_id,
            company_id=command.company_id,
            from_version=command.expected_version,
            to_version=command.expected_version + 1,
            changed=True,
            new_workflow=WorkflowType.MANUAL_REVIEW,
            new_review_reasons=(),
            trigger=command.trigger,
            executable=False,
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


async def test_next_invoice_same_vat_as_archived_hub_owned_partner_fails_closed_not_reused(session: Session) -> None:
    """P0-PROD-09A Phase 7: the exact D-Market-shaped scenario, through the REAL writer.

    Setup mirrors production after P0-PROD-08W: a prior review already has a
    ``SupplierRemediationEffect(mode=ONE_OFF_VENDOR, resolved_partner_id=448)`` --
    i.e. the Hub *does* own partner 448 -- and Odoo reports that partner as
    ``active: False`` (archived), exactly like the real post-pilot state.

    A brand-new review, same company, same VAT, resolved with
    ``SupplierResolutionMode.ONE_OFF_VENDOR`` (the natural operator choice for
    "another one-off invoice from this same vendor") does NOT reuse partner 448.
    It raises ``SupplierPartnerInactiveError`` from inside the real
    ``OdooSupplierPartnerWriter``, before ``_create_or_reuse_one_off_vendor_partner``'s
    own Hub-ownership check ever runs. No duplicate partner is created (the fake client
    asserts ``create_res_partner`` is never called), but reuse also does not happen --
    the operator is stuck with no supported next step.
    """

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

    writer, client = _real_writer()
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

    use_case = ResolveWorkbenchSupplierUseCase(
        review_reader=_FakeReviewReader(review),
        source_invoice_reader=_FakeSourceReader(source),
        resolution_validator=ValidateSupplierResolutionUseCase(
            source_invoice_reader=_FakeSourceReader(source),
            partner_reader=_FakePartnerReader(None),
        ),
        resolution_writer=SqlAlchemyReviewSupplierResolutionRepository(session),
        remediation_effect_writer=effect_repo,
        supplier_partner_writer=writer,
        reclassifier=_FakeReclassifier(),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        workbench_republisher=None,
        retirement_writer=SqlAlchemyReviewOneOffVendorRetirementRepository(session),
    )
    command = ResolveWorkbenchSupplierCommand(
        review_id=NEW_REVIEW_ID,
        company_id=COMPANY_ID,
        expected_version=1,
        mode=SupplierResolutionMode.ONE_OFF_VENDOR,
        approved_by=ACTOR,
        resolved_partner_id=None,
    )

    with pytest.raises(SupplierPartnerInactiveError):
        await use_case.execute(command)

    assert client.create_calls == []  # no duplicate partner was ever attempted
    # No new remediation effect or retirement row was committed for the new review --
    # the operator is left with no completed resolution and no supported next action.
    assert session.query(WorkbenchReviewSupplierRemediationEffect).filter_by(review_id=NEW_REVIEW_ID).count() == 0
    assert session.query(WorkbenchReviewOneOffVendorRetirement).filter_by(review_id=NEW_REVIEW_ID).count() == 0
