"""Explicit supplier remediation orchestration (P0-3D2D).

Ties P0-3D2A..C + the controlled writer (PR #129) together:
    eligibility -> immutable source -> reserve intent -> validate / gated create
    -> immutable effect -> SUPPLIER_RESOLUTION reclassification (real matcher rerun).
Never fakes a matched PartnerMatchResult; never executes a Vendor Bill.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands.supplier_partner import CreateSupplierPartnerCommand
from app.application.dto.supplier_partner import SupplierPartnerWriteResult, SupplierPartnerWriteStatus
from app.application.exceptions.supplier_partner import SupplierPartnerWriteSafetyGateError
from app.application.workbench.dto import ReviewItem, ReviewStatus
from app.application.workbench.evidence import ReviewSourceInvoiceEvidence
from app.application.workbench.exceptions import (
    ReviewNotFoundError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
    SupplierResolutionConflictError,
    SupplierResolutionContractError,
    SupplierResolutionPartnerInactiveError,
    SupplierResolutionPartnerMismatchError,
    SupplierResolutionPartnerNotFoundError,
    SupplierResolutionRaceError,
    WorkbenchCandidateAmbiguityError,
    WorkbenchProjectionPublishError,
)
from app.application.workbench.projection import ProjectionPublishResult, WorkbenchProjection
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.reclassification import (
    ReclassifyReviewCommand,
    ReviewReclassificationResult,
    ReviewReclassificationTrigger,
)
from app.application.workbench.supplier_remediation import (
    ResolveWorkbenchSupplierCommand,
    SupplierPartnerWriteEffectStatus,
    SupplierRemediationStatus,
)
from app.application.workbench.supplier_remediation_use_cases import ResolveWorkbenchSupplierUseCase
from app.application.workbench.supplier_resolution import ResolutionPartnerRecord, SupplierResolutionMode
from app.application.workbench.supplier_resolution_use_cases import ValidateSupplierResolutionUseCase
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.db.base import Base
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_supplier_remediation_effect import WorkbenchReviewSupplierRemediationEffect
from app.models.workbench_review_supplier_resolution import WorkbenchReviewSupplierResolution
from app.persistence import (
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyReviewSupplierResolutionRepository,
    SqlAlchemyUnitOfWork,
)

COMPANY_ID = 7
REVIEW_ID = "review:supplier-remediation-1"
ETTN = "AKYASAM-ETTN-REMED-1"
VKN = "0430367181"
PARTNER_ID = 4010
ACTOR = "finance.operator"


# --------------------------------------------------------------------------- builders


def _source_invoice(*, supplier_name: str = "AKYASAM", supplier_vat: str | None = VKN) -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="AKY-1",
            invoice_uuid="00000000-0000-4000-8000-00000000d001",
            ettn=ETTN,
            issue_date=date(2026, 8, 20),
            currency_code="TRY",
        ),
        supplier=Party(name=supplier_name, tax_number=supplier_vat),
        customer=Party(name="ICT TEKNOLOJI", tax_number="1112223334"),
        totals=MonetaryTotals(payable_amount=Decimal("100.00")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Yillik aidat",
                quantity=Decimal("1"),
                unit_code="C62",
                unit_price=Decimal("83.33"),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
            ),
        ),
    )


def _source_evidence(**kwargs) -> ReviewSourceInvoiceEvidence:
    return ReviewSourceInvoiceEvidence(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        review_version=1,
        source_invoice_id=ETTN,
        invoice=_source_invoice(**kwargs),
    )


def _supplier_not_found_reason() -> ManualReviewReason:
    return ManualReviewReason(
        code=ManualReviewReasonCode.SUPPLIER_NOT_FOUND,
        message="No supplier partner for VKN.",
        source="partner_matching",
        candidate_count=0,
    )


def _review_item(
    *,
    version: int = 1,
    status: ReviewStatus = ReviewStatus.PENDING_REVIEW,
    workflow: WorkflowType = WorkflowType.MANUAL_REVIEW,
    reasons: tuple[ManualReviewReason, ...] = (),
) -> ReviewItem:
    return ReviewItem(
        review_id=REVIEW_ID,
        invoice_id=ETTN,
        invoice_number="AKY-1",
        supplier_tax_number=VKN,
        supplier_name="AKYASAM",
        invoice_date=date(2026, 8, 20),
        currency="TRY",
        total_amount=Decimal("100.00"),
        workflow=workflow,
        status=status,
        review_reasons=reasons or (_supplier_not_found_reason(),),
        version=version,
    )


class _FakeReviewReader:
    def __init__(self, item: ReviewItem) -> None:
        self.item = item

    def get_review_item(self, query: ReviewDetailQuery) -> ReviewItem:
        if not isinstance(query, ReviewDetailQuery):
            raise SupplierResolutionContractError("ReviewDetailQuery is required.")
        if query.review_id != self.item.review_id or query.company_id != COMPANY_ID:
            raise ReviewNotFoundError("Review item was not found.")
        return self.item


class _FakeSourceReader:
    def __init__(self, evidence: ReviewSourceInvoiceEvidence) -> None:
        self.evidence = evidence

    def get(self, *, review_id: str, company_id: int) -> ReviewSourceInvoiceEvidence:
        if review_id != REVIEW_ID or company_id != COMPANY_ID:
            raise ReviewNotFoundError("Source invoice evidence was not found.")
        return self.evidence


class _FakePartnerReader:
    def __init__(self, partner: ResolutionPartnerRecord | None) -> None:
        self.partner = partner
        self.calls: list[int] = []

    def find_partner_by_id(self, partner_id: int) -> ResolutionPartnerRecord | None:
        self.calls.append(partner_id)
        if self.partner is not None and self.partner.id == partner_id:
            return self.partner
        return None


class _FakeSupplierPartnerWriter:
    def __init__(
        self, *, existing_vat: str | None = None, existing_partner_id: int = 5001, new_partner_id: int = 6001
    ) -> None:
        self.existing_vat = existing_vat
        self.existing_partner_id = existing_partner_id
        self.new_partner_id = new_partner_id
        self.calls: list[CreateSupplierPartnerCommand] = []
        self._created_vats: set[str] = set()
        self.gate_error: Exception | None = None

    async def create_supplier(self, command: CreateSupplierPartnerCommand) -> SupplierPartnerWriteResult:
        self.calls.append(command)
        if self.gate_error is not None:
            raise self.gate_error
        vat = command.supplier_tax_number.strip()
        if vat == self.existing_vat or vat in self._created_vats:
            return SupplierPartnerWriteResult(
                status=SupplierPartnerWriteStatus.ALREADY_EXISTS,
                partner_id=self.existing_partner_id if vat == self.existing_vat else self.new_partner_id,
                company_id=command.company_id,
                supplier_name=command.supplier_name,
                supplier_tax_number=vat,
                idempotency_key=command.idempotency_key,
                existing_by="vat",
            )
        self._created_vats.add(vat)
        return SupplierPartnerWriteResult(
            status=SupplierPartnerWriteStatus.CREATED,
            partner_id=self.new_partner_id,
            company_id=command.company_id,
            supplier_name=command.supplier_name,
            supplier_tax_number=vat,
            idempotency_key=command.idempotency_key,
        )


class _FakeReclassifier:
    """Reclassify N -> N+1, mutating the shared ReviewItem exactly like production would."""

    def __init__(self, reader: _FakeReviewReader, *, resolves: bool = True, fail: Exception | None = None) -> None:
        self._reader = reader
        self._resolves = resolves
        self._fail = fail
        self.calls: list[ReclassifyReviewCommand] = []

    async def execute(self, command: ReclassifyReviewCommand) -> ReviewReclassificationResult:
        self.calls.append(command)
        if self._fail is not None:
            raise self._fail
        item = self._reader.item
        previous_reasons = item.review_reasons
        previous_workflow = item.workflow
        if self._resolves:
            new_reasons: tuple[ManualReviewReason, ...] = ()
            new_workflow = WorkflowType.VENDOR_BILL
            changed = True
            to_version = command.expected_version + 1
        else:
            new_reasons = (_supplier_not_found_reason(),)
            new_workflow = WorkflowType.MANUAL_REVIEW
            changed = False
            to_version = command.expected_version
        self._reader.item = replace(item, version=to_version, review_reasons=new_reasons, workflow=new_workflow)
        return ReviewReclassificationResult(
            review_id=command.review_id,
            company_id=command.company_id,
            changed=changed,
            from_version=command.expected_version,
            to_version=to_version,
            previous_workflow=previous_workflow,
            new_workflow=new_workflow,
            previous_review_reasons=previous_reasons,
            new_review_reasons=new_reasons,
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
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        db_session.add(
            WorkbenchReviewItem(
                review_id=REVIEW_ID,
                company_id=COMPANY_ID,
                invoice_id=ETTN,
                invoice_number="AKY-1",
                supplier_tax_number=VKN,
                supplier_name="AKYASAM",
                invoice_date=date(2026, 8, 20),
                currency="TRY",
                total_amount=Decimal("100.00"),
                workflow="manual_review",
                status="pending_review",
                review_reasons=[{"code": "supplier_not_found", "message": "x"}],
                warnings=[],
                version=1,
                idempotency_key="uyumsoft:7:AKYASAM-ETTN-REMED-1",
            )
        )
        db_session.flush()
        yield db_session


def _partner(**kw) -> ResolutionPartnerRecord:
    return ResolutionPartnerRecord(
        id=kw.get("id", PARTNER_ID),
        name=kw.get("name", "AKYASAM"),
        vat=kw.get("vat", VKN),
        active=kw.get("active", True),
        company_id=kw.get("company_id", None),
    )


_UNSET = object()


class _FakeRepublisher:
    """Update-only Workbench republisher. Records every projection it is handed.

    Has no create path at all -- structurally incapable of adding a Workbench row.
    """

    def __init__(self, *, fail: Exception | None = None, record_id: int = 9100) -> None:
        self.fail = fail
        self.record_id = record_id
        self.calls: list[WorkbenchProjection] = []

    def republish_projection(self, projection: WorkbenchProjection) -> ProjectionPublishResult:
        self.calls.append(projection)
        if self.fail is not None:
            raise self.fail
        return ProjectionPublishResult(
            review_id=projection.review_id,
            odoo_record_id=self.record_id,
            created=False,
            updated=True,
            version=projection.version,
        )


class _Harness:
    def __init__(
        self,
        session: Session,
        *,
        review: ReviewItem | None = None,
        source: ReviewSourceInvoiceEvidence | None = None,
        partner: ResolutionPartnerRecord | None | object = _UNSET,
        writer: _FakeSupplierPartnerWriter | None = None,
        resolves: bool = True,
        reclassify_fail: Exception | None = None,
        after_precheck_hook: Any = None,
        republisher: Any = None,
    ) -> None:
        self.session = session
        self.reader = _FakeReviewReader(review or _review_item())
        self.source_reader = _FakeSourceReader(source or _source_evidence())
        resolved_partner = _partner() if partner is _UNSET else partner
        self.partner_reader = _FakePartnerReader(resolved_partner)  # type: ignore[arg-type]
        self.writer = writer or _FakeSupplierPartnerWriter()
        self.reclassifier = _FakeReclassifier(self.reader, resolves=resolves, fail=reclassify_fail)
        self.republisher = republisher
        self.resolution_repo = SqlAlchemyReviewSupplierResolutionRepository(session)
        self.effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
        self.use_case = ResolveWorkbenchSupplierUseCase(
            review_reader=self.reader,
            source_invoice_reader=self.source_reader,
            resolution_validator=ValidateSupplierResolutionUseCase(
                source_invoice_reader=self.source_reader,
                partner_reader=self.partner_reader,
            ),
            resolution_writer=self.resolution_repo,
            remediation_effect_writer=self.effect_repo,
            supplier_partner_writer=self.writer,
            reclassifier=self.reclassifier,
            unit_of_work=SqlAlchemyUnitOfWork(session),
            workbench_republisher=republisher,
            _after_precheck_hook=after_precheck_hook,
        )

    def command(self, **kw) -> ResolveWorkbenchSupplierCommand:
        base = {
            "review_id": REVIEW_ID,
            "company_id": COMPANY_ID,
            "expected_version": 1,
            "mode": SupplierResolutionMode.MATCH_EXISTING,
            "approved_by": ACTOR,
            "resolved_partner_id": PARTNER_ID,
        }
        base.update(kw)
        return ResolveWorkbenchSupplierCommand(**base)


# --------------------------------------------------------- Phase 30: MATCH_EXISTING


async def test_match_existing_resolves_and_reclassifies(session: Session) -> None:
    h = _Harness(session)
    result = await h.use_case.execute(h.command())

    assert result.status is SupplierRemediationStatus.RESOLVED
    assert (result.previous_version, result.current_version) == (1, 2)
    assert result.effective_partner_id == PARTNER_ID
    assert result.partner_write_status is SupplierPartnerWriteEffectStatus.SELECTED
    assert result.reclassified is True
    assert not any(r.code is ManualReviewReasonCode.SUPPLIER_NOT_FOUND for r in result.current_review_reasons)
    assert h.writer.calls == []  # MATCH_EXISTING never touches the writer
    assert len(h.reclassifier.calls) == 1
    assert h.reclassifier.calls[0].trigger is ReviewReclassificationTrigger.SUPPLIER_RESOLUTION
    assert (
        h.resolution_repo.get_supplier_resolution(
            review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=1
        ).resolved_partner_id
        == PARTNER_ID
    )
    assert (
        h.effect_repo.find_remediation_effect(
            review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=1
        ).partner_write_status
        is SupplierPartnerWriteEffectStatus.SELECTED
    )


async def test_match_existing_exact_replay_is_stable_already_applied(session: Session) -> None:
    h = _Harness(session)
    first = await h.use_case.execute(h.command())
    second = await h.use_case.execute(h.command())

    assert first.current_version == second.current_version == 2
    assert second.already_applied is True
    assert len(h.reclassifier.calls) == 1  # not re-run
    assert session.query(WorkbenchReviewSupplierResolution).count() == 1
    assert session.query(WorkbenchReviewSupplierRemediationEffect).count() == 1


async def test_match_existing_wrong_vat_fails_before_any_persistence(session: Session) -> None:
    h = _Harness(session, partner=_partner(vat="9999999999"))
    with pytest.raises(SupplierResolutionPartnerMismatchError):
        await h.use_case.execute(h.command())
    assert session.query(WorkbenchReviewSupplierResolution).count() == 0
    assert h.reclassifier.calls == []


async def test_match_existing_inactive_partner_fails_closed(session: Session) -> None:
    h = _Harness(session, partner=_partner(active=False))
    with pytest.raises(SupplierResolutionPartnerInactiveError):
        await h.use_case.execute(h.command())
    assert session.query(WorkbenchReviewSupplierResolution).count() == 0


async def test_match_existing_missing_partner_fails_closed(session: Session) -> None:
    h = _Harness(session, partner=None)
    with pytest.raises(SupplierResolutionPartnerNotFoundError):
        await h.use_case.execute(h.command())


async def test_stale_different_decision_conflicts(session: Session) -> None:
    h = _Harness(session)
    await h.use_case.execute(h.command())
    # reset review to v1 to simulate a racing operator sending a different decision for the same version
    h.reader.item = _review_item(version=1)
    with pytest.raises(SupplierResolutionConflictError):
        await h.use_case.execute(h.command(resolved_partner_id=9999))


async def test_terminal_review_is_rejected(session: Session) -> None:
    h = _Harness(session, review=_review_item(status=ReviewStatus.DECISION_SUBMITTED))
    with pytest.raises(ReviewStateConflictError):
        await h.use_case.execute(h.command())


async def test_stale_expected_version_conflicts(session: Session) -> None:
    h = _Harness(session, review=_review_item(version=3))
    with pytest.raises(ReviewVersionConflictError):
        await h.use_case.execute(h.command(expected_version=1))


async def test_review_without_supplier_not_found_is_rejected(session: Session) -> None:
    other = ManualReviewReason(
        code=ManualReviewReasonCode.PRODUCT_NOT_FOUND, message="x", source="product_matching", candidate_count=0
    )
    h = _Harness(session, review=_review_item(reasons=(other,)))
    with pytest.raises(SupplierResolutionContractError):
        await h.use_case.execute(h.command())


async def test_match_existing_reports_incomplete_when_supplier_still_missing(session: Session) -> None:
    h = _Harness(session, resolves=False)
    result = await h.use_case.execute(h.command())
    assert result.status is SupplierRemediationStatus.REMEDIATION_INCOMPLETE
    assert result.reclassified is False
    assert any(r.code is ManualReviewReasonCode.SUPPLIER_NOT_FOUND for r in result.current_review_reasons)


# --------------------------------------------------------- Phase 31: CREATE_PERMANENT


async def test_create_permanent_uses_immutable_source_identity_and_reclassifies(session: Session) -> None:
    h = _Harness(
        session,
        source=_source_evidence(supplier_name="AKYASAM GIDA A.S.", supplier_vat=VKN),
        partner=_partner(id=6001, vat=VKN),
        writer=_FakeSupplierPartnerWriter(new_partner_id=6001),
    )
    result = await h.use_case.execute(
        h.command(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, resolved_partner_id=None)
    )

    assert result.status is SupplierRemediationStatus.RESOLVED
    assert result.effective_partner_id == 6001
    assert result.partner_write_status is SupplierPartnerWriteEffectStatus.CREATED
    assert len(h.writer.calls) == 1
    created = h.writer.calls[0]
    assert created.supplier_name == "AKYASAM GIDA A.S."  # from immutable source, not the request
    assert created.supplier_tax_number == VKN
    effect = h.effect_repo.find_remediation_effect(review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=1)
    assert effect.mode is SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER  # mode is never rewritten to match_existing
    assert effect.resolved_partner_id == 6001
    # the reserved intent row keeps the create-permanent mode with a NULL partner id
    intent = h.resolution_repo.get_supplier_resolution(review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=1)
    assert intent.mode is SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER
    assert intent.resolved_partner_id is None


async def test_create_permanent_already_exists_records_already_exists(session: Session) -> None:
    h = _Harness(
        session,
        partner=_partner(id=5001, vat=VKN),
        writer=_FakeSupplierPartnerWriter(existing_vat=VKN, existing_partner_id=5001),
    )
    result = await h.use_case.execute(
        h.command(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, resolved_partner_id=None)
    )
    assert result.partner_write_status is SupplierPartnerWriteEffectStatus.ALREADY_EXISTS
    assert result.effective_partner_id == 5001


async def test_create_permanent_resumes_after_a_committed_reservation_and_partial_failure(session: Session) -> None:
    # Committed reservation is durable; reclassify fails -> the request errors but the
    # reservation (and the effect) stay committed so a retry can resume. A retry does not
    # create a second Odoo partner *because the writer's own exact-VAT lookup returns
    # ALREADY_EXISTS* -- see test_..._concurrent_writer_interleave_is_detection_only for the
    # residual race the writer only detects, not prevents.
    writer = _FakeSupplierPartnerWriter(new_partner_id=6001)
    h = _Harness(session, partner=_partner(id=6001, vat=VKN), writer=writer, reclassify_fail=RuntimeError("boom"))

    with pytest.raises(Exception):  # noqa: B017 - reclassify failure surfaces as a safe error
        await h.use_case.execute(
            h.command(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, resolved_partner_id=None)
        )

    assert session.query(WorkbenchReviewSupplierResolution).count() == 1  # reservation committed
    assert session.query(WorkbenchReviewSupplierRemediationEffect).count() == 1  # effect committed
    assert len(writer.calls) == 1

    h.reclassifier = _FakeReclassifier(h.reader, resolves=True)
    h.use_case._reclassifier = h.reclassifier
    result = await h.use_case.execute(
        h.command(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, resolved_partner_id=None)
    )
    assert result.status is SupplierRemediationStatus.RESOLVED
    assert result.already_applied is True
    assert len(writer.calls) == 1  # writer's own idempotency -> no second create on this retry
    assert session.query(WorkbenchReviewSupplierRemediationEffect).count() == 1


async def test_create_permanent_write_gate_disabled_blocks_before_reclassification(session: Session) -> None:
    writer = _FakeSupplierPartnerWriter()
    writer.gate_error = SupplierPartnerWriteSafetyGateError("Supplier remediation write must be explicitly enabled.")
    h = _Harness(session, writer=writer)

    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        await h.use_case.execute(
            h.command(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, resolved_partner_id=None)
        )

    assert session.query(WorkbenchReviewSupplierRemediationEffect).count() == 0
    assert h.reclassifier.calls == []


# --------------------------------------------------------- Phase 32: ONE_OFF


async def test_use_one_off_records_intent_only(session: Session) -> None:
    h = _Harness(session)
    result = await h.use_case.execute(
        h.command(mode=SupplierResolutionMode.USE_ONE_OFF_SUPPLIER, resolved_partner_id=None)
    )
    assert result.status is SupplierRemediationStatus.ONE_OFF_EXECUTION_NOT_SUPPORTED
    assert result.effective_partner_id is None
    assert (result.previous_version, result.current_version) == (1, 1)
    assert h.writer.calls == []
    assert h.reclassifier.calls == []
    assert session.query(WorkbenchReviewSupplierResolution).count() == 1
    assert session.query(WorkbenchReviewSupplierRemediationEffect).count() == 0


async def test_use_one_off_exact_replay_is_stable(session: Session) -> None:
    h = _Harness(session)
    cmd = h.command(mode=SupplierResolutionMode.USE_ONE_OFF_SUPPLIER, resolved_partner_id=None)
    await h.use_case.execute(cmd)
    again = await h.use_case.execute(cmd)
    assert again.already_applied is True
    assert session.query(WorkbenchReviewSupplierResolution).count() == 1


# --------------------------------------------------------- Phase 33: concurrency (two real transactions)


@pytest.fixture()
def shared_db_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'remed.db'}")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewSupplierResolution.__table__,
            WorkbenchReviewSupplierRemediationEffect.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as seed:
        seed.add(
            WorkbenchReviewItem(
                review_id=REVIEW_ID,
                company_id=COMPANY_ID,
                invoice_id=ETTN,
                invoice_number="AKY-1",
                supplier_tax_number=VKN,
                supplier_name="AKYASAM",
                invoice_date=date(2026, 8, 20),
                currency="TRY",
                total_amount=Decimal("100.00"),
                workflow="manual_review",
                status="pending_review",
                review_reasons=[{"code": "supplier_not_found", "message": "x"}],
                warnings=[],
                version=1,
                idempotency_key="uyumsoft:7:AKYASAM-ETTN-REMED-1",
            )
        )
        seed.commit()
    try:
        yield factory
    finally:
        engine.dispose()


async def test_reservation_barrier_stops_the_losing_concurrent_request_before_odoo(shared_db_factory) -> None:
    # Two independent sessions / transactions on the same database. Session B does its
    # pre-check (sees nothing), then session A wins and COMMITS its reservation, then B
    # attempts to reserve -> UNIQUE(review_id, review_version) violation -> the loser is
    # raised out (SupplierResolutionRaceError) BEFORE reaching SupplierPartnerWriter.
    writer = _FakeSupplierPartnerWriter(new_partner_id=6001)  # shared across both "processes"
    session_a = shared_db_factory()
    session_b = shared_db_factory()
    try:
        h_a = _Harness(session_a, partner=_partner(id=6001, vat=VKN), writer=writer)
        cmd = h_a.command(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, resolved_partner_id=None)

        async def _winner_commits_between_bs_precheck_and_reserve() -> None:
            await h_a.use_case.execute(cmd)  # A reserves + commits, creates the partner, writes the effect

        h_b = _Harness(
            session_b,
            partner=_partner(id=6001, vat=VKN),
            writer=writer,
            after_precheck_hook=_winner_commits_between_bs_precheck_and_reserve,
        )

        with pytest.raises(SupplierResolutionRaceError):
            await h_b.use_case.execute(cmd)

        # exactly one winner: one reservation, one effect, one Odoo create; B never called the writer
        assert session_b.query(WorkbenchReviewSupplierResolution).count() == 1
        assert session_b.query(WorkbenchReviewSupplierRemediationEffect).count() == 1
        assert len(writer.calls) == 1
    finally:
        session_a.close()
        session_b.close()


async def test_different_decisions_for_same_version_conflict_across_two_sessions(shared_db_factory) -> None:
    writer = _FakeSupplierPartnerWriter(new_partner_id=6001)
    session_a = shared_db_factory()
    session_b = shared_db_factory()
    try:
        h_a = _Harness(session_a, partner=_partner(id=6001, vat=VKN), writer=writer)

        async def _winner_commits() -> None:
            await h_a.use_case.execute(
                h_a.command(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, resolved_partner_id=None)
            )

        h_b = _Harness(
            session_b,
            partner=_partner(id=4010, vat=VKN),
            writer=writer,
            after_precheck_hook=_winner_commits,
        )
        with pytest.raises(SupplierResolutionConflictError):
            # B picked MATCH_EXISTING with a different partner for the same review version
            await h_b.use_case.execute(
                h_b.command(mode=SupplierResolutionMode.MATCH_EXISTING, resolved_partner_id=4010)
            )
        assert session_b.query(WorkbenchReviewSupplierResolution).count() == 1  # only A's
        assert len(writer.calls) == 1  # only A reached the writer
    finally:
        session_a.close()
        session_b.close()


async def test_concurrent_writer_interleave_is_detection_only_not_prevention() -> None:
    # DOCUMENTED RESIDUAL RACE. The Hub reservation makes exactly one request the winner
    # for a review version, but a request that legitimately RESUMES a committed reservation
    # (whose owner is still in flight) calls the controlled writer; Odoo has no VAT
    # uniqueness. If two create_supplier calls interleave inside the writer -- both search
    # 0, both create -- the post-create exact-VAT re-query DETECTS it and fails closed with
    # SupplierPartnerDuplicateRaceError. It is NOT prevented and no "one net partner" is
    # claimed. This test asserts the fail-closed detection, using the real #129 writer.
    from app.application.exceptions.supplier_partner import SupplierPartnerDuplicateRaceError
    from app.erp.write.odoo_supplier_partner_writer import (
        OdooSupplierPartnerRepository,
        OdooSupplierPartnerWritePolicy,
        OdooSupplierPartnerWriter,
    )

    class _RacyJson2Client:
        # search always returns 0 before create; the post-create re-query returns TWO rows.
        def __init__(self) -> None:
            self.created = 0

        async def create_res_partner(self, payload: dict[str, Any]) -> int:
            self.created += 1
            return 6000 + self.created

        async def search_read(self, *, model, domain, fields, limit=20, offset=0):
            has_vat_eq = any(clause[:2] == ["vat", "="] for clause in domain if isinstance(clause, list))
            if has_vat_eq and self.created == 0:
                return []
            if has_vat_eq and self.created >= 1:
                return [
                    {"id": 6001, "name": "AKYASAM", "vat": VKN, "active": True, "company_id": False},
                    {"id": 6002, "name": "AKYASAM", "vat": VKN, "active": True, "company_id": False},
                ]
            return []

    writer = OdooSupplierPartnerWriter(
        repository=OdooSupplierPartnerRepository(client=_RacyJson2Client()),
        policy=OdooSupplierPartnerWritePolicy(
            supplier_remediation_write_enabled=True, app_env="staging", odoo_host="test-ictteknoloji.odoo.com"
        ),
    )
    with pytest.raises(SupplierPartnerDuplicateRaceError):
        await writer.create_supplier(
            CreateSupplierPartnerCommand(
                company_id=COMPANY_ID,
                supplier_name="AKYASAM",
                supplier_tax_number=VKN,
                idempotency_key="supplier-remediation:7:X:1",
                approved_by=ACTOR,
            )
        )


def test_command_rejects_partner_id_for_non_match_modes() -> None:
    with pytest.raises(SupplierResolutionContractError):
        ResolveWorkbenchSupplierCommand(
            review_id=REVIEW_ID,
            company_id=COMPANY_ID,
            expected_version=1,
            mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER,
            approved_by=ACTOR,
            resolved_partner_id=10,
        )


def test_command_never_carries_supplier_identity() -> None:
    import dataclasses

    fields = {f.name for f in dataclasses.fields(ResolveWorkbenchSupplierCommand)}
    for forbidden in ("supplier_name", "supplier_vat", "supplier_tax_number", "invoice", "ettn"):
        assert forbidden not in fields


# --------------------------------------------------------- Phase 25/37: no automatic create / no execution


# --------------------------------------------------------- P0-3D2E: Workbench republish


async def test_match_existing_republishes_existing_projection_with_new_review_state(session: Session) -> None:
    republisher = _FakeRepublisher()
    h = _Harness(session, republisher=republisher)

    result = await h.use_case.execute(h.command())

    assert result.status is SupplierRemediationStatus.RESOLVED
    assert result.workbench_republished is True
    assert len(republisher.calls) == 1
    projection = republisher.calls[0]
    assert isinstance(projection, WorkbenchProjection)
    assert (projection.review_id, projection.company_id) == (REVIEW_ID, COMPANY_ID)
    assert projection.version == 2  # post-reclassification review state, not the pre-remediation version
    assert projection.workflow is WorkflowType.VENDOR_BILL


async def test_create_permanent_republishes_existing_projection(session: Session) -> None:
    writer = _FakeSupplierPartnerWriter(new_partner_id=6001)
    republisher = _FakeRepublisher()
    h = _Harness(session, partner=_partner(id=6001, vat=VKN), writer=writer, republisher=republisher)

    result = await h.use_case.execute(
        h.command(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, resolved_partner_id=None)
    )

    assert result.workbench_republished is True
    assert len(republisher.calls) == 1
    assert republisher.calls[0].version == 2


async def test_exact_retry_after_full_success_republishes_same_projection_without_recreating_supplier(
    session: Session,
) -> None:
    writer = _FakeSupplierPartnerWriter(new_partner_id=6001)
    republisher = _FakeRepublisher()
    h = _Harness(session, partner=_partner(id=6001, vat=VKN), writer=writer, republisher=republisher)
    cmd = h.command(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, resolved_partner_id=None)

    first = await h.use_case.execute(cmd)
    retry = await h.use_case.execute(cmd)

    assert first.workbench_republished is True
    assert retry.workbench_republished is True
    assert retry.already_applied is True
    assert len(writer.calls) == 1  # no supplier recreation on retry
    assert len(republisher.calls) == 2  # same row updated again, idempotently
    assert session.query(WorkbenchReviewSupplierResolution).count() == 1
    assert session.query(WorkbenchReviewSupplierRemediationEffect).count() == 1


async def test_republish_transport_failure_after_success_preserves_committed_remediation(session: Session) -> None:
    republisher = _FakeRepublisher(fail=WorkbenchProjectionPublishError("Odoo Workbench projection publish failed."))
    h = _Harness(session, republisher=republisher)

    result = await h.use_case.execute(h.command())

    assert result.status is SupplierRemediationStatus.RESOLVED
    assert result.workbench_republished is False  # not manufactured
    # remediation is committed and durable despite the republish failure
    assert session.query(WorkbenchReviewSupplierResolution).count() == 1
    assert session.query(WorkbenchReviewSupplierRemediationEffect).count() == 1


async def test_retry_after_republish_failure_republishes_without_recreating_supplier(session: Session) -> None:
    writer = _FakeSupplierPartnerWriter(new_partner_id=6001)
    republisher = _FakeRepublisher(fail=WorkbenchProjectionPublishError("transient"))
    h = _Harness(session, partner=_partner(id=6001, vat=VKN), writer=writer, republisher=republisher)
    cmd = h.command(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, resolved_partner_id=None)

    first = await h.use_case.execute(cmd)
    assert first.workbench_republished is False

    republisher.fail = None  # Workbench recovers
    retry = await h.use_case.execute(cmd)

    assert retry.workbench_republished is True
    assert retry.already_applied is True
    assert len(writer.calls) == 1  # no second supplier create
    assert session.query(WorkbenchReviewSupplierRemediationEffect).count() == 1


async def test_publisher_disabled_reports_false_and_never_touches_odoo(session: Session) -> None:
    h = _Harness(session, republisher=None)  # odoo_workbench_projection_publish_enabled = false

    result = await h.use_case.execute(h.command())

    assert result.status is SupplierRemediationStatus.RESOLVED
    assert result.workbench_republished is False


async def test_use_one_off_never_republishes(session: Session) -> None:
    republisher = _FakeRepublisher()
    h = _Harness(session, republisher=republisher)

    result = await h.use_case.execute(
        h.command(mode=SupplierResolutionMode.USE_ONE_OFF_SUPPLIER, resolved_partner_id=None)
    )

    assert result.status is SupplierRemediationStatus.ONE_OFF_EXECUTION_NOT_SUPPORTED
    assert result.workbench_republished is False
    assert republisher.calls == []


async def test_reclassification_failure_never_republishes(session: Session) -> None:
    republisher = _FakeRepublisher()
    h = _Harness(session, republisher=republisher, reclassify_fail=RuntimeError("boom"))

    with pytest.raises(Exception):  # noqa: B017 - reclassify failure surfaces as a safe error
        await h.use_case.execute(h.command())

    assert republisher.calls == []


async def test_missing_projection_identity_fails_closed_without_creating_a_replacement(session: Session) -> None:
    # The publisher's update-only lookup found no row (wrong/missing identity). The fake
    # has no create path at all, so a replacement row is structurally impossible; the
    # remediation stays committed and the result is truthfully republished=False.
    republisher = _FakeRepublisher(fail=WorkbenchProjectionPublishError("Odoo Workbench projection publish failed."))
    h = _Harness(session, republisher=republisher)

    result = await h.use_case.execute(h.command())

    assert result.workbench_republished is False
    assert not hasattr(republisher, "create")
    assert session.query(WorkbenchReviewSupplierRemediationEffect).count() == 1


async def test_concurrent_exact_retries_update_one_projection_and_never_duplicate(session: Session) -> None:
    writer = _FakeSupplierPartnerWriter(new_partner_id=6001)
    republisher = _FakeRepublisher()
    h = _Harness(session, partner=_partner(id=6001, vat=VKN), writer=writer, republisher=republisher)
    cmd = h.command(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, resolved_partner_id=None)

    await h.use_case.execute(cmd)
    await h.use_case.execute(cmd)
    await h.use_case.execute(cmd)

    # every republish targeted the same review/company; one resolution, one effect, one create
    assert {(p.review_id, p.company_id) for p in republisher.calls} == {(REVIEW_ID, COMPANY_ID)}
    assert len(writer.calls) == 1
    assert session.query(WorkbenchReviewSupplierResolution).count() == 1
    assert session.query(WorkbenchReviewSupplierRemediationEffect).count() == 1


async def test_ambiguous_projection_rows_fail_closed_as_republish_false(session: Session) -> None:
    republisher = _FakeRepublisher(fail=WorkbenchCandidateAmbiguityError("multiple rows"))
    h = _Harness(session, republisher=republisher)

    result = await h.use_case.execute(h.command())

    assert result.workbench_republished is False
    assert session.query(WorkbenchReviewSupplierRemediationEffect).count() == 1


def test_orchestration_module_republish_is_update_only_never_a_create_path() -> None:
    from pathlib import Path

    source = Path("app/application/workbench/supplier_remediation_use_cases.py").read_text(encoding="utf-8")
    # The orchestration must call the update-only republish, never a create-capable
    # projection operation.
    assert "republish_projection" in source
    assert "publish_projection" not in source.replace("republish_projection", "")
    for token in ("create_studio_record", "create_projection", ".create(", "publisher.publish"):
        assert token not in source


def test_orchestration_module_never_executes_a_vendor_bill() -> None:
    from pathlib import Path

    source = Path("app/application/workbench/supplier_remediation_use_cases.py").read_text(encoding="utf-8")
    for token in (
        "account.move",
        "VendorBillExecution",
        "ExecutionPlanner",
        "RunAcceptedDecisionExecution",
        "action_post",
    ):
        assert token not in source


@pytest.mark.parametrize(
    "module_path",
    [
        "app/application/use_cases/import_invoice.py",
        "app/application/use_cases/reclassify_review.py",
        "app/application/workbench/reclassification.py",
        "app/matching/partner.py",
    ],
)
def test_import_and_reclassification_never_reach_the_supplier_writer(module_path: str) -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(Path(module_path).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for name in imported:
        assert "supplier_partner_writer" not in name
        assert "supplier_remediation" not in name
        assert "odoo_supplier_partner_writer" not in name
