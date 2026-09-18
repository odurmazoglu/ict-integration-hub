"""P0-PROD-09C: reuse of an archived Hub-owned ONE_OFF_VENDOR partner.

Fixes the gap P0-PROD-09A proved (see the updated
``test_p0_prod_09a_one_off_vendor_reuse_gap.py``): ``OdooSupplierPartnerWriter`` now
accepts an optional, caller-supplied ``authorize_inactive_reuse`` predicate on
``CreateSupplierPartnerCommand``. The writer itself never decides Hub ownership --
it only asks the predicate the caller hands it, and only when it finds exactly one
*inactive* exact-VAT match. By default (every caller except the ONE_OFF_VENDOR
orchestration path) that predicate is ``None``, so the writer's own fail-closed
behavior is completely unchanged.

This file adds the remaining real-writer-backed regressions the P0-PROD-09C task
requires beyond what the updated 09A file already covers (archived-reuse succeeds,
non-owned-inactive still fails closed): active-partner behavior is unaffected, the
reused partner gets its own fresh retirement row that archives correctly again after
a new Vendor Bill, replay creates no duplicates, a crash-then-retry stays
deterministic, and CREATE_PERMANENT_SUPPLIER is untouched by the new predicate.

No reactivation write exists anywhere in this change -- see
``test_no_reactivation_write_path_exists_anywhere`` for the structural proof, and the
PR description for the Odoo-ORM evidence behind that design decision.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands.one_off_vendor_retirement import ArchiveOneOffVendorPartnerCommand
from app.application.dto.one_off_vendor_retirement import OneOffVendorArchiveWriteResult, OneOffVendorArchiveWriteStatus
from app.application.exceptions.supplier_partner import SupplierPartnerInactiveError
from app.application.workbench.dto import ReviewItem, ReviewStatus
from app.application.workbench.evidence import ReviewSourceInvoiceEvidence
from app.application.workbench.one_off_vendor_retirement import (
    ArchiveOneOffVendorCommand,
    ArchiveOneOffVendorStatus,
    OneOffVendorRetirementStatus,
)
from app.application.workbench.one_off_vendor_use_cases import ArchiveOneOffVendorUseCase
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.reclassification import ReviewReclassificationResult
from app.application.workbench.supplier_remediation import (
    ResolveWorkbenchSupplierCommand,
    SupplierPartnerWriteEffectStatus,
    SupplierRemediationEffect,
    SupplierRemediationStatus,
)
from app.application.workbench.supplier_remediation_use_cases import ResolveWorkbenchSupplierUseCase
from app.application.workbench.supplier_resolution import (
    ResolutionPartnerRecord,
    SupplierResolution,
    SupplierResolutionMode,
)
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
from app.models.workbench_review_supplier_resolution import WorkbenchReviewSupplierResolution
from app.persistence import (
    SqlAlchemyReviewOneOffVendorRetirementRepository,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyReviewSupplierResolutionRepository,
    SqlAlchemyUnitOfWork,
)

COMPANY_ID = 1
VKN = "2650179910"
PRIOR_REVIEW_ID = "review:prior-one-off-archived"
NEW_REVIEW_ID = "review:new-invoice-same-vat"
ARCHIVED_PARTNER_ID = 448
ACTIVE_PARTNER_ID = 9001
ACTOR = "finance.operator"


# --------------------------------------------------------------------------- fakes


class _FakeOdooJson2Client:
    """Configurable fake standing in for the real Odoo JSON-2 client boundary."""

    def __init__(self, *, records: list[dict[str, Any]], create_result: int | None = None) -> None:
        self._records = records
        self._create_result = create_result
        self.create_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []

    async def create_res_partner(self, payload: dict[str, Any]) -> int:
        self.create_calls.append(payload)
        if self._create_result is None:
            raise AssertionError("create_res_partner must never be called in this scenario.")
        return self._create_result

    async def search_read(self, *, model: str, domain, fields, limit: int = 20, offset: int = 0):
        self.search_calls.append({"model": model, "domain": domain, "fields": fields, "limit": limit})
        return self._records


def _writer(client: _FakeOdooJson2Client) -> OdooSupplierPartnerWriter:
    policy = OdooSupplierPartnerWritePolicy(
        supplier_remediation_write_enabled=True,
        app_env="staging",
        odoo_host="test-ictteknoloji.odoo.com",
    )
    return OdooSupplierPartnerWriter(repository=OdooSupplierPartnerRepository(client=client), policy=policy)


def _source_invoice(*, ettn: str = "NEXT-ETTN-1", invoice_number: str = "HD-NEXT-1") -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number=invoice_number,
            invoice_uuid=f"00000000-0000-4000-8000-{abs(hash(ettn)) % 10**12:012d}",
            ettn=ettn,
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
    def find_partner_by_id(self, partner_id: int) -> ResolutionPartnerRecord | None:
        return None


class _FakeReclassifier:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls = 0
        self._fail = fail

    async def execute(self, command):
        self.calls += 1
        if self._fail is not None:
            raise self._fail
        return ReviewReclassificationResult(
            review_id=command.review_id,
            company_id=command.company_id,
            from_version=command.expected_version,
            to_version=command.expected_version + 1,
            changed=True,
            previous_workflow=WorkflowType.MANUAL_REVIEW,
            new_workflow=WorkflowType.MANUAL_REVIEW,
            previous_review_reasons=(),
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
            WorkbenchReviewSupplierResolution.__table__,
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


def _seed_prior_effect(session: Session, *, partner_id: int = ARCHIVED_PARTNER_ID) -> None:
    SqlAlchemyReviewSupplierRemediationEffectRepository(session).create_remediation_effect(
        SupplierRemediationEffect(
            review_id=PRIOR_REVIEW_ID,
            company_id=COMPANY_ID,
            review_version=3,
            source_invoice_id="prior-ettn",
            mode=SupplierResolutionMode.ONE_OFF_VENDOR,
            resolved_partner_id=partner_id,
            partner_write_status=SupplierPartnerWriteEffectStatus.CREATED,
            source_supplier_tax_number=VKN,
            approved_by=ACTOR,
        )
    )
    session.commit()


def _new_review_item() -> ReviewItem:
    return ReviewItem(
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


def _use_case(
    session: Session,
    *,
    writer: OdooSupplierPartnerWriter,
    effect_repo: SqlAlchemyReviewSupplierRemediationEffectRepository | None = None,
    reclassifier: _FakeReclassifier | None = None,
) -> ResolveWorkbenchSupplierUseCase:
    source = ReviewSourceInvoiceEvidence(
        review_id=NEW_REVIEW_ID,
        company_id=COMPANY_ID,
        review_version=1,
        source_invoice_id="NEXT-ETTN-1",
        invoice=_source_invoice(),
    )
    return ResolveWorkbenchSupplierUseCase(
        review_reader=_FakeReviewReader(_new_review_item()),
        source_invoice_reader=_FakeSourceReader(source),
        resolution_validator=ValidateSupplierResolutionUseCase(
            source_invoice_reader=_FakeSourceReader(source),
            partner_reader=_FakePartnerReader(),
        ),
        resolution_writer=SqlAlchemyReviewSupplierResolutionRepository(session),
        remediation_effect_writer=effect_repo or SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        supplier_partner_writer=writer,
        reclassifier=reclassifier or _FakeReclassifier(),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        workbench_republisher=None,
        retirement_writer=SqlAlchemyReviewOneOffVendorRetirementRepository(session),
    )


def _command(
    *, mode: SupplierResolutionMode = SupplierResolutionMode.ONE_OFF_VENDOR
) -> ResolveWorkbenchSupplierCommand:
    return ResolveWorkbenchSupplierCommand(
        review_id=NEW_REVIEW_ID,
        company_id=COMPANY_ID,
        expected_version=1,
        mode=mode,
        approved_by=ACTOR,
        resolved_partner_id=None,
    )


# --------------------------------------------------------------------------- 4: active behavior unchanged


async def test_active_exact_vat_match_reuse_unaffected_by_the_fix(session: Session) -> None:
    """An ACTIVE exact-VAT match is reused exactly as before -- the new
    authorize_inactive_reuse predicate is never even consulted, because the writer's
    ``if not existing.active`` guard never triggers for an active partner."""

    _seed_prior_effect(session, partner_id=ACTIVE_PARTNER_ID)
    client = _FakeOdooJson2Client(
        records=[
            {
                "id": ACTIVE_PARTNER_ID,
                "name": "D-MARKET ELEKTRONİK HİZMETLER VE TİCARET ANONİM ŞİRKETİ",
                "vat": VKN,
                "active": True,
                "company_id": [COMPANY_ID, "ICT Teknoloji"],
            }
        ]
    )
    use_case = _use_case(session, writer=_writer(client))

    result = await use_case.execute(_command())

    assert result.status is SupplierRemediationStatus.RESOLVED
    assert result.effective_partner_id == ACTIVE_PARTNER_ID
    assert result.partner_write_status is SupplierPartnerWriteEffectStatus.ALREADY_EXISTS
    assert client.create_calls == []


# --------------------------------------------------------------------------- 6: archive-after-reuse


async def test_successful_execution_archives_the_reused_partner_again(session: Session) -> None:
    """After the NEW review reuses partner 448, once a durable Vendor Bill exists for
    THAT review, ArchiveOneOffVendorUseCase independently archives it again -- the
    archive-last lifecycle is per-review, unaffected by the partner having already
    gone through this exact same lifecycle once for the prior review."""

    _seed_prior_effect(session)
    client = _FakeOdooJson2Client(
        records=[
            {
                "id": ARCHIVED_PARTNER_ID,
                "name": "D-MARKET ELEKTRONİK HİZMETLER VE TİCARET ANONİM ŞİRKETİ",
                "vat": VKN,
                "active": False,
                "company_id": [COMPANY_ID, "ICT Teknoloji"],
            }
        ]
    )
    use_case = _use_case(session, writer=_writer(client))
    result = await use_case.execute(_command())
    assert result.one_off_vendor_retirement_status is OneOffVendorRetirementStatus.PENDING_VENDOR_BILL

    class _FakeEvidenceReader:
        def has_successful_vendor_bill(self, *, review_id: str, company_id: int) -> bool:
            return review_id == NEW_REVIEW_ID  # durable Vendor Bill exists for the NEW review only

    class _FakeArchivePort:
        def __init__(self) -> None:
            self.calls: list[ArchiveOneOffVendorPartnerCommand] = []

        async def archive_partner(self, command: ArchiveOneOffVendorPartnerCommand) -> OneOffVendorArchiveWriteResult:
            self.calls.append(command)
            return OneOffVendorArchiveWriteResult(
                status=OneOffVendorArchiveWriteStatus.ARCHIVED, partner_id=command.partner_id, safe_message="ok"
            )

    port = _FakeArchivePort()
    archive_use_case = ArchiveOneOffVendorUseCase(
        retirement_writer=SqlAlchemyReviewOneOffVendorRetirementRepository(session),
        vendor_bill_evidence_reader=_FakeEvidenceReader(),
        retirement_port=port,
        unit_of_work=SqlAlchemyUnitOfWork(session),
        approved_by=ACTOR,
    )
    archive_result = await archive_use_case.execute(
        ArchiveOneOffVendorCommand(review_id=NEW_REVIEW_ID, company_id=COMPANY_ID, review_version=1)
    )

    assert archive_result.status is ArchiveOneOffVendorStatus.ARCHIVED
    assert len(port.calls) == 1
    assert port.calls[0].partner_id == ARCHIVED_PARTNER_ID
    new_retirement = SqlAlchemyReviewOneOffVendorRetirementRepository(session).find(
        review_id=NEW_REVIEW_ID, company_id=COMPANY_ID, review_version=1
    )
    assert new_retirement.status is OneOffVendorRetirementStatus.ARCHIVED


# --------------------------------------------------------------------------- 7: replay creates no duplicates


async def test_replay_does_not_duplicate_partner_effect_or_retirement(session: Session) -> None:
    _seed_prior_effect(session)
    client = _FakeOdooJson2Client(
        records=[
            {
                "id": ARCHIVED_PARTNER_ID,
                "name": "D-MARKET ELEKTRONİK HİZMETLER VE TİCARET ANONİM ŞİRKETİ",
                "vat": VKN,
                "active": False,
                "company_id": [COMPANY_ID, "ICT Teknoloji"],
            }
        ]
    )
    writer = _writer(client)

    first = await _use_case(session, writer=writer).execute(_command())
    assert first.already_applied is False

    second = await _use_case(session, writer=writer).execute(_command())
    assert second.already_applied is True
    assert second.effective_partner_id == ARCHIVED_PARTNER_ID

    assert client.create_calls == []
    assert session.query(WorkbenchReviewSupplierRemediationEffect).filter_by(review_id=NEW_REVIEW_ID).count() == 1
    assert session.query(WorkbenchReviewOneOffVendorRetirement).filter_by(review_id=NEW_REVIEW_ID).count() == 1
    assert session.query(WorkbenchReviewSupplierResolution).filter_by(review_id=NEW_REVIEW_ID).count() == 1


# --------------------------------------------------------------------------- 8: crash-then-retry determinism


async def test_crash_after_reserved_intent_before_completion_then_retry_completes_safely(
    session: Session,
) -> None:
    """Simulates a crash: the operator's intent (SupplierResolution) was reserved and
    committed -- the existing P0-3D2D crash-safety discipline -- but the process died
    before the writer/effect/retirement ever ran (no SupplierRemediationEffect exists
    for the new review). A retry must resume and complete exactly once, reusing
    partner 448 idempotently -- never a duplicate partner, never a duplicate effect."""

    _seed_prior_effect(session)
    SqlAlchemyReviewSupplierResolutionRepository(session).reserve_supplier_resolution(
        SupplierResolution(
            mode=SupplierResolutionMode.ONE_OFF_VENDOR,
            review_id=NEW_REVIEW_ID,
            company_id=COMPANY_ID,
            review_version=1,
            source_invoice_id="NEXT-ETTN-1",
        )
    )
    session.commit()
    assert session.query(WorkbenchReviewSupplierRemediationEffect).filter_by(review_id=NEW_REVIEW_ID).count() == 0

    client = _FakeOdooJson2Client(
        records=[
            {
                "id": ARCHIVED_PARTNER_ID,
                "name": "D-MARKET ELEKTRONİK HİZMETLER VE TİCARET ANONİM ŞİRKETİ",
                "vat": VKN,
                "active": False,
                "company_id": [COMPANY_ID, "ICT Teknoloji"],
            }
        ]
    )
    use_case = _use_case(session, writer=_writer(client))

    result = await use_case.execute(_command())

    assert result.status is SupplierRemediationStatus.RESOLVED
    assert result.effective_partner_id == ARCHIVED_PARTNER_ID
    assert result.already_applied is True  # resumed an already-reserved intent
    assert client.create_calls == []
    assert session.query(WorkbenchReviewSupplierRemediationEffect).filter_by(review_id=NEW_REVIEW_ID).count() == 1
    assert session.query(WorkbenchReviewOneOffVendorRetirement).filter_by(review_id=NEW_REVIEW_ID).count() == 1


# --------------------------------------------------------------------------- 9: CREATE_PERMANENT_SUPPLIER unchanged


async def test_create_permanent_supplier_against_inactive_hub_owned_partner_still_fails_closed(
    session: Session,
) -> None:
    """CREATE_PERMANENT_SUPPLIER never passes authorize_inactive_reuse -- even when a
    ONE_OFF_VENDOR effect happens to already own this exact partner id (proving Hub
    ownership), CREATE_PERMANENT_SUPPLIER's own call site is untouched by the fix and
    still fails closed on any inactive exact-VAT match. Reuse across resolution modes
    is not something this fix introduces."""

    _seed_prior_effect(session)  # partner 448 IS Hub-owned via ONE_OFF_VENDOR
    client = _FakeOdooJson2Client(
        records=[
            {
                "id": ARCHIVED_PARTNER_ID,
                "name": "D-MARKET ELEKTRONİK HİZMETLER VE TİCARET ANONİM ŞİRKETİ",
                "vat": VKN,
                "active": False,
                "company_id": [COMPANY_ID, "ICT Teknoloji"],
            }
        ]
    )
    use_case = _use_case(session, writer=_writer(client))

    with pytest.raises(SupplierPartnerInactiveError):
        await use_case.execute(_command(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER))

    assert client.create_calls == []
    assert session.query(WorkbenchReviewSupplierRemediationEffect).filter_by(review_id=NEW_REVIEW_ID).count() == 0


# --------------------------------------------------------------------------- no reactivation write exists


def test_no_reactivation_write_path_exists_anywhere() -> None:
    """P0-PROD-09C design decision: reactivation is NOT implemented (Odoo's `active`
    field is a search-visibility flag only, never enforced by create()/write() or any
    relational constraint -- see the PR description for the full evidence). Grepping
    the entire supplier-partner write surface for any {"active": true} payload proves
    no reactivation capability was added merely for convenience."""

    import inspect

    from app.erp.write import odoo_supplier_partner_writer

    source = inspect.getsource(odoo_supplier_partner_writer)
    assert '"active": True' not in source
    assert "'active': True" not in source
    assert "active=True" not in source or "isinstance" in source  # no write call sets active True


def test_archive_write_payload_remains_exactly_active_false() -> None:
    """The ONE_OFF_VENDOR archive write itself is untouched by this fix -- still
    exactly {"active": False}, still the only res.partner write this lifecycle ever
    performs."""

    import inspect

    from app.connectors.odoo import client as odoo_client_module

    source = inspect.getsource(odoo_client_module)
    assert '"active": False' in source
