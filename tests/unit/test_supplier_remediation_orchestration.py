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
)
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
    ) -> None:
        self.reader = _FakeReviewReader(review or _review_item())
        self.source_reader = _FakeSourceReader(source or _source_evidence())
        resolved_partner = _partner() if partner is _UNSET else partner
        self.partner_reader = _FakePartnerReader(resolved_partner)  # type: ignore[arg-type]
        self.writer = writer or _FakeSupplierPartnerWriter()
        self.reclassifier = _FakeReclassifier(self.reader, resolves=resolves, fail=reclassify_fail)
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


async def test_create_permanent_resumes_after_effect_write_failure_without_second_partner(session: Session) -> None:
    writer = _FakeSupplierPartnerWriter(new_partner_id=6001)
    h = _Harness(session, partner=_partner(id=6001, vat=VKN), writer=writer, reclassify_fail=RuntimeError("boom"))

    with pytest.raises(Exception):  # noqa: B017 - reclassify failure surfaces as a safe error
        await h.use_case.execute(
            h.command(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, resolved_partner_id=None)
        )

    # reservation + effect were written; the Odoo partner exists once
    assert session.query(WorkbenchReviewSupplierResolution).count() == 1
    assert session.query(WorkbenchReviewSupplierRemediationEffect).count() == 1
    assert len(writer.calls) == 1

    # retry with a healthy reclassifier resumes without creating a second partner
    h.reclassifier = _FakeReclassifier(h.reader, resolves=True)
    h.use_case._reclassifier = h.reclassifier
    result = await h.use_case.execute(
        h.command(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, resolved_partner_id=None)
    )
    assert result.status is SupplierRemediationStatus.RESOLVED
    assert result.already_applied is True
    assert len(writer.calls) == 1  # no second create
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


# --------------------------------------------------------- Phase 33: concurrency


async def test_concurrent_create_permanent_duplicate_request_creates_one_effect_one_partner(session: Session) -> None:
    writer = _FakeSupplierPartnerWriter(new_partner_id=6001)
    h = _Harness(session, partner=_partner(id=6001, vat=VKN), writer=writer)
    cmd = h.command(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, resolved_partner_id=None)
    await h.use_case.execute(cmd)
    h.reader.item = _review_item(version=1)  # simulate the second racing worker still seeing v1
    await h.use_case.execute(cmd)
    assert session.query(WorkbenchReviewSupplierRemediationEffect).count() == 1
    assert len(writer.calls) == 1


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
