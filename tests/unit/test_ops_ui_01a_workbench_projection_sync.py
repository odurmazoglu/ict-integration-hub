"""OPS-UI-01A: canonical Hub -> Odoo Workbench projection synchronization.

Real SQLAlchemy persistence and the real PR #197 decision flows (VİTEL human
selection, LOGOSOFT automatic match, account-only, dismiss) feed the real
``WorkbenchProjectionSynchronizer`` and ``OdooWorkbenchProjectionPublisher``. Odoo
is a Studio fake that reads values back the way Odoo does (Many2one as
``[id, name]``, sanitizer-wrapped HTML, integer defaults of 0), so idempotency is
proven against realistic read shapes, not against the values we wrote.
"""

from __future__ import annotations

import inspect
import io
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.execution import ExecutionArtifact, ExecutionArtifactType, ExecutionState
from app.application.execution.contracts import AcceptedReviewDecision, ExecutionMode
from app.application.execution.workbench_customer_quotation import WorkbenchAcceptedDecisionExecutionDispatcher
from app.application.execution.workbench_vendor_bill import (
    EXECUTION_PROJECTION_FAILURE_MESSAGE,
    WorkbenchVendorBillExecutionResult,
    WorkbenchVendorBillExecutionStatus,
)
from app.application.use_cases.import_invoice import ImportInvoiceUseCase
from app.application.workbench import (
    LineResolution,
    ReviewDecisionType,
    ReviewStatus,
    SubmitReviewDecisionUseCase,
)
from app.application.workbench.accounting_resolution_use_cases import SubmitReviewAccountingResolutionUseCase
from app.application.workbench.dto import ReviewReasonsRole
from app.application.workbench.operating_expense_mapping_use_cases import SubmitOperatingExpenseMappingUseCase
from app.application.workbench.ports import ReviewDecisionWriter, ReviewItemWriter, ReviewReclassificationWriter
from app.application.workbench.projection_sync import WorkbenchProjectionSources, WorkbenchProjectionSynchronizer
from app.application.workbench.projection_sync_contracts import (
    PROJECTION_SYNC_WARNING,
    ProjectionSyncOutcome,
    ProjectionSyncResult,
)
from app.application.workbench.reclassification import ReviewReclassificationTrigger
from app.application.workbench.source_identity_correction_use_cases import CorrectReviewSourceIdentityUseCase
from app.application.workbench.supplier_remediation_use_cases import ResolveWorkbenchSupplierUseCase
from app.application.workflow import WorkflowType
from app.cli.reconcile_workbench_projection import list_review_ids, run_reconcile
from app.db.base import Base
from app.erp.odoo.workbench_projection_publisher import (
    OdooWorkbenchProjectionFieldMapping,
    OdooWorkbenchProjectionPublisher,
)
from app.models.execution_source_invoice_evidence import ExecutionSourceInvoiceEvidence
from app.models.workbench_review_classification_evidence import WorkbenchReviewClassificationEvidence
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workflow_execution import WorkflowExecution, WorkflowExecutionEvent, WorkflowExecutionStep
from app.persistence import (
    SqlAlchemyExecutionSourceInvoiceReader,
    SqlAlchemyReviewRepository,
    SqlAlchemyUnitOfWork,
)
from app.persistence.review_execution_evidence_reader import SqlAlchemyReviewExecutionEvidenceReader
from tests.unit.test_p0_prod_19f_review_lifecycle_effective_state import (
    COMPANY_ID,
    EXPENSE_ACCOUNT_ID,
    LOGOSOFT_BASIC_PRODUCT_ID,
    LOGOSOFT_SKU,
    PRODUCT_NOT_FOUND_REASON,
    REVIEW_ID,
    VITEL_PRODUCT_ID,
    VITEL_SKU,
    _accept,
    _Accounts,
    _command,
    _Products,
    _row_counts,
    _seed,
)

MODEL = "x_ipp_import_workbench"
TRY_CURRENCY_ID = 31
VENDOR_BILL_ID = 60

# The deployed Studio selections (OPS-UI-01 audit): "Customer Quotation" is not a workflow value.
STUDIO_SELECTIONS = {
    "x_studio_review_status": ("Pending Review", "Decision Submitted", "Resolved", "Dismissed"),
    "x_studio_workflow": ("Vendor Bill", "RFQ", "Expense", "Asset", "Subscription", "Manual Review"),
    "x_studio_execution_status": ("Executed", "Already Executed"),
}
MANY2ONE_FIELDS = frozenset({"x_studio_company", "x_studio_currency_id", "x_studio_vendor_bill"})
INTEGER_FIELDS = frozenset({"x_studio_review_version"})
HTML_FIELDS = frozenset({"x_studio_review_reasons", "x_studio_warnings"})


# --------------------------------------------------------------------------- fixtures / fakes


@pytest.fixture()
def session(tmp_path) -> Session:
    """File-backed SQLite: a fresh session is a real separate connection that only sees committed data."""

    engine = create_engine(f"sqlite:///{tmp_path / 'hub.db'}")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewExecutionEvidence.__table__,
            WorkbenchReviewDecision.__table__,
            ExecutionSourceInvoiceEvidence.__table__,
            WorkbenchReviewClassificationEvidence.__table__,
            WorkflowExecution.__table__,
            WorkflowExecutionStep.__table__,
            WorkflowExecutionEvent.__table__,
        ],
    )
    with sessionmaker(bind=engine)() as db_session:
        yield db_session
    engine.dispose()


class StudioFake:
    """In-memory Odoo Studio model that reads values back in Odoo's shapes."""

    def __init__(self, *, currencies: list[dict[str, Any]] | None = None) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.currencies = currencies if currencies is not None else [{"id": TRY_CURRENCY_ID, "name": "TRY"}]
        self.creates: list[dict[str, Any]] = []
        self.writes: list[tuple[int, dict[str, Any]]] = []
        self.currency_reads = 0
        self._next_id = 1

    def search_read(self, *, model: str, domain: list[Any], fields: list[str], limit: int, offset: int = 0):
        if model == "res.currency":
            self.currency_reads += 1
            code = domain[0][2]
            return tuple(dict(currency) for currency in self.currencies if currency["name"] == code)[:limit]
        assert model == MODEL
        wanted = {clause[0]: clause[2] for clause in domain}
        matches = [
            (record_id, row)
            for record_id, row in self.rows.items()
            if all(row.get(field_name) == value for field_name, value in wanted.items())
        ]
        return tuple(self._read_shape(record_id, row, fields) for record_id, row in matches)[:limit]

    def create(self, *, model: str, values: dict[str, Any]) -> int:
        assert model == MODEL
        record_id = self._next_id
        self._next_id += 1
        self.creates.append(dict(values))
        self.rows[record_id] = self._stored(values)
        return record_id

    def write(self, *, model: str, record_id: int, values: dict[str, Any]) -> None:
        assert model == MODEL
        self.writes.append((record_id, dict(values)))
        self.rows[record_id].update(self._stored(values))

    def read_selection_values(self, *, model: str, field_name: str) -> tuple[str, ...]:
        return STUDIO_SELECTIONS.get(field_name, ())

    @property
    def write_count(self) -> int:
        return len(self.creates) + len(self.writes)

    @staticmethod
    def _stored(values: dict[str, Any]) -> dict[str, Any]:
        stored = dict(values)
        for field_name in HTML_FIELDS & stored.keys():
            if stored[field_name]:
                # Odoo's HTML sanitizer wraps stored markup in an attribute-less element.
                stored[field_name] = f"<span>{stored[field_name]}</span>"
        return stored

    @staticmethod
    def _read_shape(record_id: int, row: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        shaped: dict[str, Any] = {}
        for field_name in fields:
            if field_name == "id":
                shaped["id"] = record_id
                continue
            value = row.get(field_name)
            if field_name in MANY2ONE_FIELDS:
                shaped[field_name] = [value, f"Record {value}"] if value else False
            elif field_name in INTEGER_FIELDS:
                shaped[field_name] = value or 0
            else:
                shaped[field_name] = False if value is None else value
        return shaped


class Snapshots:
    def __init__(self, snapshot=None) -> None:
        self.snapshot = snapshot

    def find_latest_snapshot_for_review(self, *, review_id: str, company_id: int):
        return self.snapshot


class RecordingSynchronizer:
    def __init__(self, outcome: ProjectionSyncOutcome = ProjectionSyncOutcome.UPDATED) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, int]] = []

    def sync(self, *, review_id: str, company_id: int) -> ProjectionSyncResult:
        self.calls.append((review_id, company_id))
        return ProjectionSyncResult(
            review_id=review_id,
            outcome=self.outcome,
            applied=self.outcome is not ProjectionSyncOutcome.ERROR,
            error="Injected projection failure" if self.outcome is ProjectionSyncOutcome.ERROR else None,
        )

    plan = sync


class RaisingPublisher:
    def sync_projection(self, projection, *, apply: bool):
        raise RuntimeError("odoo is down")


def _mapping() -> OdooWorkbenchProjectionFieldMapping:
    return OdooWorkbenchProjectionFieldMapping(
        model=MODEL,
        name="x_name",
        review_id="x_studio_review_id",
        company_id="x_studio_company",
        invoice_number="x_studio_invoice_number",
        supplier="x_studio_supplier",
        supplier_tax_number="x_studio_supplier_tax_number",
        invoice_date="x_studio_invoice_date",
        currency="x_studio_currency",
        invoice_total="x_studio_invoice_total",
        review_status="x_studio_review_status",
        workflow="x_studio_workflow",
        review_version="x_studio_review_version",
        last_sync_at="x_studio_last_sync_at",
        review_reasons="x_studio_review_reasons",
        warnings="x_studio_warnings",
        execution_status="x_studio_execution_status",
        vendor_bill="x_studio_vendor_bill",
        execution_message="x_studio_execution_message",
        currency_id="x_studio_currency_id",
    )


def _synchronizer(db: Session, studio: StudioFake, *, snapshot=None, publisher=None) -> WorkbenchProjectionSynchronizer:
    """A synchronizer whose every call reads through its own fresh session (never ``db``)."""

    engine = db.get_bind()

    @contextmanager
    def read_scope():
        read_session = Session(bind=engine)
        try:
            repository = SqlAlchemyReviewRepository(read_session)
            yield WorkbenchProjectionSources(
                review_reader=repository,
                accepted_decision_reader=repository,
                accepted_source_reader=SqlAlchemyExecutionSourceInvoiceReader(read_session),
                execution_snapshot_reader=Snapshots(snapshot),
                publisher=publisher or OdooWorkbenchProjectionPublisher(adapter=studio, mapping=_mapping()),
            )
        finally:
            read_session.rollback()
            read_session.close()

    return WorkbenchProjectionSynchronizer(read_scope=read_scope)


def _seed_committed(db: Session, **kwargs: Any) -> None:
    _seed(db, **kwargs)
    db.commit()


def _execution_snapshot(
    *,
    state: ExecutionState = ExecutionState.COMPLETED,
    artifact_id: str | None = str(VENDOR_BILL_ID),
    decision_version: int = 2,
    failure: str | None = None,
    retry_count: int = 0,
):
    artifacts = (
        (
            ExecutionArtifact(
                artifact_type=ExecutionArtifactType.VENDOR_BILL,
                artifact_id=artifact_id,
                external_identity="vendor-bill-write:ops-ui-01a",
                created=True,
            ),
        )
        if artifact_id is not None
        else ()
    )
    step = SimpleNamespace(last_result=SimpleNamespace(produced_artifacts=artifacts), retry_count=retry_count)
    return SimpleNamespace(
        execution_id="accepted-decision-execution:ops-ui-01a",
        decision_version=decision_version,
        state=state,
        steps=(step,),
        retry_policy=SimpleNamespace(max_attempts=3),
        failure=SimpleNamespace(safe_message=failure) if failure else None,
    )


def _row(studio: StudioFake) -> dict[str, Any]:
    assert len(studio.rows) == 1
    return next(iter(studio.rows.values()))


def _accept_vitel(db: Session) -> None:
    _seed_committed(db, product_id=None, seller_item_code=VITEL_SKU, reasons=(PRODUCT_NOT_FOUND_REASON,))
    _accept(db, _command((LineResolution(line_number="1", selected_product_id=VITEL_PRODUCT_ID),)))


# --------------------------------------------------------------------------- 1. pending review


def test_pending_review_projects_current_blockers_as_warning_badges(session: Session) -> None:
    _seed_committed(session, product_id=None, seller_item_code=VITEL_SKU, reasons=(PRODUCT_NOT_FOUND_REASON,))
    studio = StudioFake()

    result = _synchronizer(session, studio).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert result.outcome is ProjectionSyncOutcome.CREATED and result.applied
    row = _row(studio)
    assert row["x_studio_review_status"] == "Pending Review"
    assert row["x_studio_review_version"] == 1
    assert row["x_studio_workflow"] == "Vendor Bill"
    assert row["x_studio_invoice_total"] == 120.0
    assert row["x_studio_currency"] == "TRY" and row["x_studio_currency_id"] == TRY_CURRENCY_ID
    assert "text-bg-warning" in row["x_studio_review_reasons"]
    assert PRODUCT_NOT_FOUND_REASON.message in row["x_studio_review_reasons"]
    assert "Decision basis" not in row["x_studio_review_reasons"]
    assert row["x_studio_execution_status"] is None and row["x_studio_vendor_bill"] is None
    assert row["x_studio_review_id"] == REVIEW_ID and row["x_studio_company"] == COMPANY_ID


# --------------------------------------------------------------------------- 2-6. decided reviews


def test_human_selected_product_decision_projects_decision_basis_and_effective_product(session: Session) -> None:
    _accept_vitel(session)
    studio = StudioFake()

    _synchronizer(session, studio).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)

    row = _row(studio)
    assert row["x_studio_review_status"] == "Decision Submitted"
    assert row["x_studio_review_version"] == 2
    reasons = row["x_studio_review_reasons"]
    assert "Decision basis — accepted decision v2" in reasons
    assert "PRODUCT_NOT_FOUND" in reasons
    assert f"Line 1 → product {VITEL_PRODUCT_ID} (human selected)" in reasons
    assert "text-bg-warning" not in reasons
    # The Hub's historical reasons are never rewritten by projecting them.
    stored = session.scalar(select(WorkbenchReviewItem.review_reasons))
    assert stored[0]["code"] == "PRODUCT_NOT_FOUND"


def test_automatic_product_match_decision_projects_automatic_source(session: Session) -> None:
    _seed_committed(session, product_id=LOGOSOFT_BASIC_PRODUCT_ID, seller_item_code=LOGOSOFT_SKU)
    _accept(session, _command(()))
    studio = StudioFake()

    _synchronizer(session, studio).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)

    reasons = _row(studio)["x_studio_review_reasons"]
    assert f"Line 1 → product {LOGOSOFT_BASIC_PRODUCT_ID} (automatic)" in reasons
    assert "human selected" not in reasons


def test_account_only_decision_projects_account_resolution(session: Session) -> None:
    _seed_committed(session, product_id=None, seller_item_code=VITEL_SKU, reasons=(PRODUCT_NOT_FOUND_REASON,))
    _accept(
        session,
        _command((LineResolution(line_number="1", account_only=True, expense_account_id=EXPENSE_ACCOUNT_ID),)),
    )
    studio = StudioFake()

    _synchronizer(session, studio).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert f"Line 1 → account {EXPENSE_ACCOUNT_ID} (account only)" in _row(studio)["x_studio_review_reasons"]


def test_dismissed_decision_projects_dismissed_history_without_effective_lines(session: Session) -> None:
    _seed_committed(session, product_id=None, seller_item_code=VITEL_SKU, reasons=(PRODUCT_NOT_FOUND_REASON,))
    _accept(session, _command(decision=ReviewDecisionType.DISMISS))
    studio = StudioFake()

    _synchronizer(session, studio).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)

    row = _row(studio)
    assert row["x_studio_review_status"] == "Dismissed"
    # No selected workflow on a dismissal: the review's own workflow is shown.
    assert row["x_studio_workflow"] == "Vendor Bill"
    assert "Decision basis — dismissed (decision v2)" in row["x_studio_review_reasons"]
    assert "→" not in row["x_studio_review_reasons"]
    assert "text-bg-warning" not in row["x_studio_review_reasons"]


def test_decided_review_projects_selected_workflow_without_mutating_hub_workflow(session: Session) -> None:
    _seed_committed(session, product_id=None, seller_item_code=VITEL_SKU, reasons=(PRODUCT_NOT_FOUND_REASON,))
    session.execute(WorkbenchReviewItem.__table__.update().values(workflow=WorkflowType.MANUAL_REVIEW.value))
    _accept(session, _command((LineResolution(line_number="1", selected_product_id=VITEL_PRODUCT_ID),)))
    studio = StudioFake()

    projection = _synchronizer(session, studio).build_projection(review_id=REVIEW_ID, company_id=COMPANY_ID)
    _synchronizer(session, studio).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert projection.workflow is WorkflowType.VENDOR_BILL
    assert projection.review_reasons_role is ReviewReasonsRole.DECISION_BASIS
    assert _row(studio)["x_studio_workflow"] == "Vendor Bill"
    assert session.scalar(select(WorkbenchReviewItem.workflow)) == WorkflowType.MANUAL_REVIEW.value


def test_same_reasons_render_as_blockers_when_pending_and_as_history_when_decided(session: Session) -> None:
    _seed_committed(session, product_id=None, seller_item_code=VITEL_SKU, reasons=(PRODUCT_NOT_FOUND_REASON,))
    pending_studio = StudioFake()
    _synchronizer(session, pending_studio).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)
    _accept(session, _command((LineResolution(line_number="1", selected_product_id=VITEL_PRODUCT_ID),)))
    decided_studio = StudioFake()
    _synchronizer(session, decided_studio).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)

    pending, decided = _row(pending_studio)["x_studio_review_reasons"], _row(decided_studio)["x_studio_review_reasons"]
    assert "text-bg-warning" in pending and "text-bg-secondary" not in pending
    assert "text-bg-secondary" in decided and "text-bg-warning" not in decided
    assert "not open blockers" in decided


# --------------------------------------------------------------------------- 7-9. execution


def test_completed_execution_projects_executed_and_vendor_bill_from_stored_state(session: Session) -> None:
    _accept_vitel(session)
    studio = StudioFake()

    _synchronizer(session, studio, snapshot=_execution_snapshot()).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)

    row = _row(studio)
    assert row["x_studio_execution_status"] == "Executed"
    assert row["x_studio_vendor_bill"] == VENDOR_BILL_ID
    message = row["x_studio_execution_message"]
    assert message.startswith("Stored Hub execution state: completed (decision v2).")
    assert f"Odoo record {VENDOR_BILL_ID}" in message and "not a live Odoo readback" in message
    for invented in ("VERIFIED", "verified", "posted", "draft", "monetary", "RESALE"):
        assert invented not in message


def test_already_executed_replay_projects_the_stored_completed_state_and_is_idempotent(session: Session) -> None:
    """``ALREADY_EXECUTED`` is a replayed call, not a stored state: the snapshot shows ``Executed``."""

    _accept_vitel(session)
    studio = StudioFake()
    synchronizer = _synchronizer(session, studio, snapshot=_execution_snapshot())
    dispatcher = _dispatcher(synchronizer, status=WorkbenchVendorBillExecutionStatus.EXECUTED)
    dispatcher.execute(review_id=REVIEW_ID, company_id=COMPANY_ID, decision_version=2, mode=ExecutionMode.EXECUTE)
    writes_after_first = studio.write_count

    replay = _dispatcher(synchronizer, status=WorkbenchVendorBillExecutionStatus.ALREADY_EXECUTED)
    result = replay.execute(review_id=REVIEW_ID, company_id=COMPANY_ID, decision_version=2, mode=ExecutionMode.EXECUTE)

    assert result.status is WorkbenchVendorBillExecutionStatus.ALREADY_EXECUTED
    assert result.message == "Execution already completed for this accepted Vendor Bill decision."
    assert _row(studio)["x_studio_execution_status"] == "Executed"
    assert studio.write_count == writes_after_first


@pytest.mark.parametrize("state", [ExecutionState.WAITING_RETRY, ExecutionState.FAILED])
def test_failed_or_waiting_execution_projects_a_factual_stored_state_message(
    session: Session, state: ExecutionState
) -> None:
    _accept_vitel(session)
    studio = StudioFake()
    snapshot = _execution_snapshot(state=state, failure="Odoo request timed out.", retry_count=1)

    _synchronizer(session, studio, snapshot=snapshot).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)

    row = _row(studio)
    assert row["x_studio_execution_status"] is None
    # A Vendor Bill artifact stored before the failure is still a fact worth showing.
    assert row["x_studio_vendor_bill"] == VENDOR_BILL_ID
    message = row["x_studio_execution_message"]
    assert message.startswith(f"Stored Hub execution state: {state.value} (decision v2).")
    assert "Retry count 1 of max attempts 3." in message
    assert "Last failure: Odoo request timed out." in message


def test_execution_of_an_older_decision_version_is_not_projected(session: Session) -> None:
    _accept_vitel(session)
    studio = StudioFake()

    _synchronizer(session, studio, snapshot=_execution_snapshot(decision_version=1)).sync(
        review_id=REVIEW_ID, company_id=COMPANY_ID
    )

    assert _row(studio)["x_studio_execution_status"] is None and _row(studio)["x_studio_vendor_bill"] is None


# --------------------------------------------------------------------------- 10. failure after commit


def test_projection_failure_after_decision_commit_keeps_the_committed_decision(session: Session) -> None:
    _seed_committed(session, product_id=None, seller_item_code=VITEL_SKU, reasons=(PRODUCT_NOT_FOUND_REASON,))
    synchronizer = _synchronizer(session, StudioFake(), publisher=RaisingPublisher())

    acknowledgement = SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        selected_product_reader=_Products(),
        selected_account_reader=_Accounts(),
        projection_synchronizer=synchronizer,
    ).execute(_command((LineResolution(line_number="1", selected_product_id=VITEL_PRODUCT_ID),)))

    assert acknowledgement.accepted is True
    assert acknowledgement.warnings == (PROJECTION_SYNC_WARNING,)
    session.expire_all()
    assert session.scalar(select(WorkbenchReviewItem.status)) == ReviewStatus.DECISION_SUBMITTED.value
    assert session.scalar(select(WorkbenchReviewItem.version)) == 2
    assert session.scalar(select(WorkbenchReviewDecision.decision_type)) == ReviewDecisionType.SELECT_WORKFLOW.value


def test_projection_failure_after_execution_keeps_outcome_and_adds_only_a_warning() -> None:
    synchronizer = RecordingSynchronizer(ProjectionSyncOutcome.ERROR)

    executed = _dispatcher(synchronizer, status=WorkbenchVendorBillExecutionStatus.EXECUTED).execute(
        review_id=REVIEW_ID, company_id=COMPANY_ID, decision_version=2, mode=ExecutionMode.EXECUTE
    )
    failed = _dispatcher(
        synchronizer, status=WorkbenchVendorBillExecutionStatus.EXECUTION_FAILED, message="Odoo request timed out."
    ).execute(review_id=REVIEW_ID, company_id=COMPANY_ID, decision_version=2, mode=ExecutionMode.EXECUTE)

    assert executed.status is WorkbenchVendorBillExecutionStatus.EXECUTED
    assert executed.message == EXECUTION_PROJECTION_FAILURE_MESSAGE
    assert failed.status is WorkbenchVendorBillExecutionStatus.EXECUTION_FAILED
    assert failed.message == f"Odoo request timed out. {EXECUTION_PROJECTION_FAILURE_MESSAGE}"
    assert synchronizer.calls == [(REVIEW_ID, COMPANY_ID), (REVIEW_ID, COMPANY_ID)]


def test_dispatcher_does_not_sync_dry_runs_or_outcomes_without_stored_execution() -> None:
    synchronizer = RecordingSynchronizer()

    _dispatcher(
        synchronizer, status=WorkbenchVendorBillExecutionStatus.DRY_RUN_COMPLETED, mode=ExecutionMode.DRY_RUN
    ).execute(review_id=REVIEW_ID, company_id=COMPANY_ID, decision_version=2, mode=ExecutionMode.DRY_RUN)
    _dispatcher(synchronizer, status=WorkbenchVendorBillExecutionStatus.APPROVAL_REQUIRED, execution_id=None).execute(
        review_id=REVIEW_ID, company_id=COMPANY_ID, decision_version=2, mode=ExecutionMode.EXECUTE
    )

    assert synchronizer.calls == []


def test_synchronizer_never_raises_and_reports_error(session: Session) -> None:
    _accept_vitel(session)

    result = _synchronizer(session, StudioFake(), publisher=RaisingPublisher()).sync(
        review_id=REVIEW_ID, company_id=COMPANY_ID
    )

    assert result.outcome is ProjectionSyncOutcome.ERROR and result.failed and not result.applied


async def test_reclassification_use_cases_sync_after_success_and_never_on_failure() -> None:
    command = SimpleNamespace(review_id=REVIEW_ID, company_id=COMPANY_ID)
    for use_case_type in (SubmitOperatingExpenseMappingUseCase, SubmitReviewAccountingResolutionUseCase):
        synchronizer = RecordingSynchronizer(ProjectionSyncOutcome.ERROR)
        use_case = use_case_type.__new__(use_case_type)
        use_case._projection_synchronizer = synchronizer
        committed = object()

        async def succeed(cmd, *, _result=committed):
            return _result

        use_case._execute = succeed
        assert await use_case.execute(command) is committed
        assert synchronizer.calls == [(REVIEW_ID, COMPANY_ID)]

        async def fail(cmd):
            raise RuntimeError("business failure")

        use_case._execute = fail
        with pytest.raises(RuntimeError):
            await use_case.execute(command)
        assert synchronizer.calls == [(REVIEW_ID, COMPANY_ID)]


async def test_import_syncs_after_commit_and_a_projection_failure_is_only_a_warning() -> None:
    from app.application.commands import ImportInvoiceCommand
    from tests.unit.test_import_invoice_use_case import (
        FakeDecisionEngine,
        FakeImportHistory,
        RecordingReviewItemCreationService,
        RecordingUnitOfWork,
        _invoice,
        _matched_classification,
        _review_required_decision,
    )

    order: list[str] = []

    class OrderedFailingSynchronizer(RecordingSynchronizer):
        def sync(self, *, review_id: str, company_id: int) -> ProjectionSyncResult:
            order.append("sync")
            return super().sync(review_id=review_id, company_id=company_id)

    synchronizer = OrderedFailingSynchronizer(ProjectionSyncOutcome.ERROR)
    unit_of_work = RecordingUnitOfWork(order=order)
    use_case = ImportInvoiceUseCase(
        import_history=FakeImportHistory(),
        decision_engine=FakeDecisionEngine(_review_required_decision(classification_result=_matched_classification())),
        review_item_creation_service=RecordingReviewItemCreationService(order=order),
        unit_of_work=unit_of_work,
        workbench_projection_synchronizer=synchronizer,
    )

    result = await use_case.execute(
        ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN", company_id=7)
    )

    assert order == ["persist", "commit", "sync"]
    assert unit_of_work.rollbacks == 0
    assert result.review_id is not None and result.review_required is True
    assert synchronizer.calls == [(result.review_id, 7)]
    assert "Odoo Workbench projection publish failed; Hub review remains authoritative." in result.warnings


# --------------------------------------------------------------------------- 11-13. idempotency / stale


def test_repeated_identical_sync_performs_no_odoo_write(session: Session) -> None:
    _accept_vitel(session)
    studio = StudioFake()
    synchronizer = _synchronizer(session, studio, snapshot=_execution_snapshot())
    synchronizer.sync(review_id=REVIEW_ID, company_id=COMPANY_ID)
    writes = studio.write_count

    second = synchronizer.sync(review_id=REVIEW_ID, company_id=COMPANY_ID)
    third = synchronizer.plan(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert second.outcome is ProjectionSyncOutcome.NO_CHANGE and not second.applied
    assert third.outcome is ProjectionSyncOutcome.NO_CHANGE
    assert studio.write_count == writes


def test_last_sync_at_alone_never_causes_a_write(session: Session) -> None:
    _accept_vitel(session)
    studio = StudioFake()
    synchronizer = _synchronizer(session, studio)
    synchronizer.sync(review_id=REVIEW_ID, company_id=COMPANY_ID)
    _row(studio)["x_studio_last_sync_at"] = "2020-01-01 00:00:00"

    assert synchronizer.sync(review_id=REVIEW_ID, company_id=COMPANY_ID).outcome is ProjectionSyncOutcome.NO_CHANGE
    assert _row(studio)["x_studio_last_sync_at"] == "2020-01-01 00:00:00"


def test_older_review_snapshot_cannot_overwrite_a_newer_odoo_projection(session: Session) -> None:
    _accept_vitel(session)
    studio = StudioFake()
    _synchronizer(session, studio).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)
    _row(studio)["x_studio_review_version"] = 5
    writes = studio.write_count

    result = _synchronizer(session, studio).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert result.outcome is ProjectionSyncOutcome.SKIPPED_STALE and not result.applied
    assert "newer than the Hub snapshot version 2" in result.error
    assert studio.write_count == writes


def test_equal_review_version_still_receives_newer_execution_state(session: Session) -> None:
    _accept_vitel(session)
    studio = StudioFake()
    _synchronizer(session, studio).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)
    assert _row(studio)["x_studio_vendor_bill"] is None

    result = _synchronizer(session, studio, snapshot=_execution_snapshot()).sync(
        review_id=REVIEW_ID, company_id=COMPANY_ID
    )

    assert result.outcome is ProjectionSyncOutcome.UPDATED
    changed = {change.field for change in result.changes}
    assert changed == {"x_studio_execution_status", "x_studio_vendor_bill", "x_studio_execution_message"}
    assert _row(studio)["x_studio_vendor_bill"] == VENDOR_BILL_ID
    assert set(studio.writes[-1][1]) == changed | {"x_studio_last_sync_at"}


def test_equal_review_version_snapshot_cannot_clear_stored_execution_facts(session: Session) -> None:
    _accept_vitel(session)
    studio = StudioFake()
    _synchronizer(session, studio, snapshot=_execution_snapshot()).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)
    writes = studio.write_count

    result = _synchronizer(session, studio, snapshot=None).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert result.outcome is ProjectionSyncOutcome.SKIPPED_STALE
    assert studio.write_count == writes and _row(studio)["x_studio_vendor_bill"] == VENDOR_BILL_ID


# --------------------------------------------------------------------------- 14-16. currency


def test_currency_resolves_to_exactly_one_odoo_currency_per_sync(session: Session) -> None:
    _accept_vitel(session)
    studio = StudioFake()
    synchronizer = _synchronizer(session, studio)

    synchronizer.sync(review_id=REVIEW_ID, company_id=COMPANY_ID)
    synchronizer.sync(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert _row(studio)["x_studio_currency_id"] == TRY_CURRENCY_ID
    # One exact read-only lookup per sync (each sync has its own read scope and publisher).
    assert studio.currency_reads == 2


@pytest.mark.parametrize(
    ("currencies", "error"),
    [
        ([], "No Odoo currency matches invoice currency TRY."),
        (
            [{"id": 31, "name": "TRY"}, {"id": 99, "name": "TRY"}],
            "Invoice currency TRY matches more than one Odoo currency.",
        ),
    ],
)
def test_missing_or_ambiguous_currency_fails_that_review_explicitly(session: Session, currencies, error) -> None:
    _accept_vitel(session)
    studio = StudioFake(currencies=currencies)

    result = _synchronizer(session, studio).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert result.outcome is ProjectionSyncOutcome.ERROR
    assert result.error == error
    assert studio.write_count == 0


# --------------------------------------------------------------------------- 17-20. reconcile CLI


def test_reconcile_dry_run_performs_zero_writes_and_reports_field_differences(session: Session) -> None:
    _accept_vitel(session)
    studio = StudioFake()
    hub_rows_before = _row_counts(session)
    out = io.StringIO()

    report = run_reconcile(
        _synchronizer(session, studio),
        review_ids=list_review_ids(SqlAlchemyReviewRepository(session), company_id=COMPANY_ID),
        company_id=COMPANY_ID,
        apply=False,
        out=out,
    )

    assert studio.write_count == 0 and studio.rows == {}
    assert _row_counts(session) == hub_rows_before
    assert not session.new and not session.dirty and not session.deleted
    assert [result.outcome for result in report.results] == [ProjectionSyncOutcome.CREATED]
    assert not report.results[0].applied
    text = out.getvalue()
    assert "DRY-RUN (no Odoo or Hub writes)" in text
    assert f"CREATE        {REVIEW_ID} v2" in text
    assert "x_studio_review_status: None -> 'Decision Submitted'" in text
    assert "Totals: CREATE=1 UPDATE=0 NO_CHANGE=0 SKIPPED_STALE=0 ERROR=0 | reviews=1 | applied=False" in text


def test_reconcile_apply_creates_then_reports_no_change(session: Session) -> None:
    _accept_vitel(session)
    studio = StudioFake()

    created = run_reconcile(
        _synchronizer(session, studio), review_ids=(REVIEW_ID,), company_id=COMPANY_ID, apply=True, out=io.StringIO()
    )
    again = run_reconcile(
        _synchronizer(session, studio), review_ids=(REVIEW_ID,), company_id=COMPANY_ID, apply=True, out=io.StringIO()
    )

    assert created.results[0].outcome is ProjectionSyncOutcome.CREATED and created.results[0].applied
    assert len(studio.creates) == 1
    assert again.results[0].outcome is ProjectionSyncOutcome.NO_CHANGE
    assert len(studio.creates) == 1 and studio.writes == []


def test_reconcile_apply_updates_a_stale_existing_row(session: Session) -> None:
    """The D-Market shape: an Odoo row frozen at v1 / Pending Review while the Hub moved on."""

    _seed_committed(session, product_id=None, seller_item_code=VITEL_SKU, reasons=(PRODUCT_NOT_FOUND_REASON,))
    studio = StudioFake()
    _synchronizer(session, studio).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)
    _accept(session, _command((LineResolution(line_number="1", selected_product_id=VITEL_PRODUCT_ID),)))

    report = run_reconcile(
        _synchronizer(session, studio, snapshot=_execution_snapshot()),
        review_ids=(REVIEW_ID,),
        company_id=COMPANY_ID,
        apply=True,
        out=io.StringIO(),
    )

    result = report.results[0]
    assert result.outcome is ProjectionSyncOutcome.UPDATED and result.applied
    row = _row(studio)
    assert (row["x_studio_review_status"], row["x_studio_review_version"]) == ("Decision Submitted", 2)
    assert (row["x_studio_execution_status"], row["x_studio_vendor_bill"]) == ("Executed", VENDOR_BILL_ID)
    assert len(studio.creates) == 1 and len(studio.writes) == 1


def test_one_review_error_does_not_stop_reconcile_of_the_remaining_reviews(session: Session) -> None:
    _accept_vitel(session)
    studio = StudioFake()
    out = io.StringIO()

    report = run_reconcile(
        _synchronizer(session, studio),
        review_ids=("review:missing", REVIEW_ID),
        company_id=COMPANY_ID,
        apply=True,
        out=out,
    )

    assert [result.outcome for result in report.results] == [ProjectionSyncOutcome.ERROR, ProjectionSyncOutcome.CREATED]
    assert report.has_errors
    assert "ERROR         review:missing" in out.getvalue()
    assert "ERROR=1" in out.getvalue() and "CREATE=1" in out.getvalue()
    assert len(studio.creates) == 1


def test_reconcile_cli_main_is_dry_run_by_default_and_exits_nonzero_on_errors(monkeypatch, session: Session) -> None:
    from app.cli import reconcile_workbench_projection as cli

    calls: list[bool] = []

    def fake_run(synchronizer, *, review_ids, company_id, apply, out):
        calls.append(apply)
        report = cli.ReconcileReport(apply=apply)
        report.results.append(
            ProjectionSyncResult(review_id="r", outcome=ProjectionSyncOutcome.ERROR, applied=False, error="x")
        )
        return report

    monkeypatch.setattr(cli, "run_reconcile", fake_run)
    monkeypatch.setattr(
        "app.composition.imports.build_workbench_projection_synchronizer", lambda **kwargs: RecordingSynchronizer()
    )

    assert cli.main(["--company", "1"], out=io.StringIO(), engine=session.get_bind()) == 1
    assert cli.main(["--company", "1", "--apply"], out=io.StringIO(), engine=session.get_bind()) == 1
    assert calls == [False, True]


def test_reconcile_cli_reports_incomplete_mapping_as_configuration_error(monkeypatch, session: Session, capsys) -> None:
    from app.cli import reconcile_workbench_projection as cli

    for suffix in ("PARENT_MODEL", "NAME_FIELD", "REVIEW_ID_FIELD"):
        monkeypatch.delenv(f"ODOO_WORKBENCH_PUBLISHER_{suffix}", raising=False)
    out = io.StringIO()

    exit_code = cli.main(["--company", "1"], out=out, engine=session.get_bind())

    assert exit_code == cli.EXIT_CONFIGURATION_ERROR
    assert "Configuration error: model mapping is required." in capsys.readouterr().err
    assert out.getvalue() == ""


# --------------------------------------------------------------------------- 21. compatibility


def test_customer_quotation_workflow_is_not_representable_and_fails_explicitly(session: Session) -> None:
    _accept_vitel(session)
    studio = StudioFake()
    synchronizer = _synchronizer(session, studio)
    projection = replace(
        synchronizer.build_projection(review_id=REVIEW_ID, company_id=COMPANY_ID),
        workflow=WorkflowType.CUSTOMER_QUOTATION,
    )
    publisher = OdooWorkbenchProjectionPublisher(adapter=studio, mapping=_mapping())

    with pytest.raises(Exception, match="has no selection value 'Customer Quotation'"):
        publisher.sync_projection(projection, apply=True)
    assert studio.write_count == 0


def test_legacy_publish_projection_contract_is_unchanged_for_projections_without_snapshot_role() -> None:
    from tests.unit.test_odoo_workbench_projection_publisher import RecordingProjectionAdapter, _projection

    adapter = RecordingProjectionAdapter(search_records=[])
    OdooWorkbenchProjectionPublisher(adapter=adapter, mapping=_mapping()).publish_projection(
        _projection(review_reasons=(PRODUCT_NOT_FOUND_REASON,))
    )

    assert adapter.create_calls == 1
    reasons = adapter.create_values["x_studio_review_reasons"]
    assert reasons == (f'<span class="badge rounded-pill text-bg-warning">{PRODUCT_NOT_FOUND_REASON.message}</span>')
    # The legacy path writes no OPS-UI-01A snapshot fields.
    assert "x_studio_currency_id" not in adapter.create_values


def test_sync_projection_rejects_legacy_partial_projections() -> None:
    from tests.unit.test_odoo_workbench_projection_publisher import RecordingProjectionAdapter, _projection

    publisher = OdooWorkbenchProjectionPublisher(
        adapter=RecordingProjectionAdapter(search_records=[]), mapping=_mapping()
    )

    with pytest.raises(Exception, match="full-snapshot projection"):
        publisher.sync_projection(_projection(), apply=True)


# --------------------------------------------------------------------------- 22. business behaviour unchanged


def test_synchronizer_does_not_change_persisted_decision_or_review_state(session: Session) -> None:
    def persisted(db: Session) -> tuple[Any, ...]:
        item = db.scalars(select(WorkbenchReviewItem)).one()
        decision = db.scalars(select(WorkbenchReviewDecision)).one()
        return (
            item.status,
            item.version,
            item.workflow,
            item.review_reasons,
            decision.decision_type,
            decision.selected_workflow,
            decision.line_resolutions,
            decision.review_version_before,
            decision.review_version_after,
        )

    _accept_vitel(session)
    baseline = persisted(session)
    counts = _row_counts(session)
    studio = StudioFake()

    _synchronizer(session, studio, snapshot=_execution_snapshot()).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)
    run_reconcile(
        _synchronizer(session, studio), review_ids=(REVIEW_ID,), company_id=COMPANY_ID, apply=True, out=io.StringIO()
    )

    assert persisted(session) == baseline
    assert _row_counts(session) == counts
    assert not session.new and not session.dirty and not session.deleted


# --------------------------------------------------------------------------- architecture guard


def _public_methods(protocol: type) -> set[str]:
    return {name for name, value in vars(protocol).items() if callable(value) and not name.startswith("_")}


def test_review_mutation_ports_are_exactly_the_synchronized_transitions() -> None:
    """Every write port that changes a review row is covered by a synchronized use case.

    Adding a new review-mutating port method or reclassification trigger fails this
    test, forcing the new transition to be wired to the projection synchronizer.
    """

    assert _public_methods(ReviewItemWriter) == {
        "create_review_item",
        "create_review_item_with_execution_evidence",
        "create_review_item_with_classification_evidence",
        "create_review_item_with_billing_evidence",
        "create_review_item_with_execution_and_billing_evidence",
    }
    assert _public_methods(ReviewReclassificationWriter) == {"reclassify_review"}
    assert _public_methods(ReviewDecisionWriter) == {
        "has_matching_review_decision",
        "submit_review_decision",
        "submit_review_decision_with_execution_evidence",
        "submit_review_decision_with_execution_and_billing_evidence",
    }
    reclassifying_use_cases = {
        ReviewReclassificationTrigger.SUPPLIER_RESOLUTION: (ResolveWorkbenchSupplierUseCase,),
        ReviewReclassificationTrigger.MASTER_DATA_CHANGED: (
            SubmitOperatingExpenseMappingUseCase,
            SubmitReviewAccountingResolutionUseCase,
        ),
        ReviewReclassificationTrigger.SOURCE_IDENTITY_CORRECTED: (CorrectReviewSourceIdentityUseCase,),
    }
    assert set(reclassifying_use_cases) == set(ReviewReclassificationTrigger)
    synchronized = [
        (ImportInvoiceUseCase, "workbench_projection_synchronizer"),
        (SubmitReviewDecisionUseCase, "projection_synchronizer"),
        (WorkbenchAcceptedDecisionExecutionDispatcher, "projection_synchronizer"),
        *((use_case, "projection_synchronizer") for group in reclassifying_use_cases.values() for use_case in group),
    ]
    for use_case, parameter in synchronized:
        assert parameter in inspect.signature(use_case.__init__).parameters, use_case.__name__


def _dispatcher(
    synchronizer,
    *,
    status: WorkbenchVendorBillExecutionStatus,
    mode: ExecutionMode = ExecutionMode.EXECUTE,
    execution_id: str | None = "accepted-decision-execution:ops-ui-01a",
    message: str | None = None,
) -> WorkbenchAcceptedDecisionExecutionDispatcher:
    if message is None and status is WorkbenchVendorBillExecutionStatus.ALREADY_EXECUTED:
        message = "Execution already completed for this accepted Vendor Bill decision."

    class _Decisions:
        def get_accepted_decision(self, *, review_id: str, company_id: int, decision_version: int):
            return AcceptedReviewDecision(
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
                decision_id="decision:ops-ui-01a",
                selected_workflow=WorkflowType.VENDOR_BILL,
            )

    class _Workflow:
        def execute(self, *, review_id, company_id, decision_version, mode, approval=None, trace_id=None, **kwargs):
            return WorkbenchVendorBillExecutionResult(
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
                mode=mode,
                status=status,
                execution_id=execution_id,
                message=message,
            )

    return WorkbenchAcceptedDecisionExecutionDispatcher(
        accepted_decision_reader=_Decisions(),
        vendor_bill_workflow=_Workflow(),
        customer_quotation_workflow=_Workflow(),
        projection_synchronizer=synchronizer,
    )


# --------------------------------------------------------------------------- connector / async boundary


async def test_selection_metadata_read_is_a_fixed_read_only_query() -> None:
    import json

    import httpx

    from app.connectors.odoo.client import OdooJson2Client

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{"id": 1, "value": "Vendor Bill"}, {"id": 2, "value": "Manual Review"}])

    client = OdooJson2Client(
        base_url="https://example.odoo.com",
        database="db",
        api_key="key",
        timeout_seconds=5,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.odoo.com"),
    )

    values = await client.read_field_selection_values(model=MODEL, field_name="x_studio_workflow")

    assert values == ("Vendor Bill", "Manual Review")
    assert len(requests) == 1 and requests[0].url.path == "/json/2/ir.model.fields.selection/search_read"
    assert json.loads(requests[0].content)["domain"] == [
        ["field_id.model", "=", MODEL],
        ["field_id.name", "=", "x_studio_workflow"],
    ]
    with pytest.raises(Exception, match="not allowed"):
        await client.read_field_selection_values(model="account.move.bogus", field_name="state")


async def test_supplier_remediation_projects_through_the_synchronizer_off_the_event_loop() -> None:
    import threading

    loop_thread = threading.get_ident()
    seen_threads: list[int] = []

    class ThreadRecordingSynchronizer(RecordingSynchronizer):
        def sync(self, *, review_id: str, company_id: int) -> ProjectionSyncResult:
            seen_threads.append(threading.get_ident())
            return super().sync(review_id=review_id, company_id=company_id)

    command = SimpleNamespace(review_id=REVIEW_ID, company_id=COMPANY_ID)
    for outcome, republished in (
        (ProjectionSyncOutcome.UPDATED, True),
        (ProjectionSyncOutcome.NO_CHANGE, True),
        (ProjectionSyncOutcome.SKIPPED_STALE, False),
        (ProjectionSyncOutcome.ERROR, False),
    ):
        use_case = ResolveWorkbenchSupplierUseCase.__new__(ResolveWorkbenchSupplierUseCase)
        use_case._projection_synchronizer = ThreadRecordingSynchronizer(outcome)
        use_case._workbench_republisher = None
        assert await use_case._republish_workbench_projection(command) is republished

    assert seen_threads and all(thread != loop_thread for thread in seen_threads)


class RecordingLogger:
    def __init__(self) -> None:
        self.errors: list[tuple[str, dict[str, Any]]] = []

    def error(self, message: str, **kwargs: Any) -> None:
        self.errors.append((message, kwargs))

    def info(self, message: str, **kwargs: Any) -> None:
        pass

    def warning(self, message: str, **kwargs: Any) -> None:
        pass


def test_classified_odoo_failure_reports_the_safe_cause_chain_without_a_traceback(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.application.workbench import projection_sync
    from app.erp.exceptions import ErpRepositoryError

    class UnreachableStudio(StudioFake):
        def search_read(self, **kwargs):
            raise ErpRepositoryError("Odoo returned HTTP 303.")

    log = RecordingLogger()
    monkeypatch.setattr(projection_sync, "logger", log)
    _accept_vitel(session)

    result = _synchronizer(session, UnreachableStudio()).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert result.outcome is ProjectionSyncOutcome.ERROR
    assert result.error == "Odoo Workbench projection lookup failed. Odoo returned HTTP 303."
    assert log.errors == [
        (
            "workbench.projection.sync_failed",
            {
                "extra": {"review_id": REVIEW_ID, "company_id": COMPANY_ID, "apply": True, "error": result.error},
                "exc_info": False,
            },
        )
    ]


def test_unexpected_failure_is_logged_with_its_traceback(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.application.workbench import projection_sync

    log = RecordingLogger()
    monkeypatch.setattr(projection_sync, "logger", log)
    _accept_vitel(session)

    result = _synchronizer(session, StudioFake(), publisher=RaisingPublisher()).sync(
        review_id=REVIEW_ID, company_id=COMPANY_ID
    )

    assert result.error == "Odoo Workbench projection sync failed."
    assert [(message, kwargs["exc_info"]) for message, kwargs in log.errors] == [
        ("workbench.projection.sync_failed", True)
    ]


# --------------------------------------------------------------------------- session / thread boundary


def _composed(db: Session, studio: StudioFake) -> WorkbenchProjectionSynchronizer:
    """The production composition, with only the Odoo adapter and field mapping substituted."""

    from app.composition.imports import build_workbench_projection_synchronizer
    from app.core.config import Settings

    return build_workbench_projection_synchronizer(
        session=db, settings=Settings(), projection_adapter=studio, mapping=_mapping()
    )


def _record_read_sessions(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    import threading

    from app.composition import imports

    opened: list[dict[str, Any]] = []
    real_open = imports.open_read_only_session

    @contextmanager
    def recording_open(engine):
        with real_open(engine) as read_session:
            record = {"session": read_session, "opened_in": threading.get_ident(), "closed_in": None}
            opened.append(record)
            try:
                yield read_session
            finally:
                record["closed_in"] = threading.get_ident()
                record["in_transaction_at_close"] = read_session.in_transaction()

    monkeypatch.setattr(imports, "open_read_only_session", recording_open)
    return opened


async def test_worker_thread_sync_never_uses_the_request_session_and_owns_its_read_session(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    from sqlalchemy import event

    _accept_vitel(session)
    request_session_executions: list[int] = []
    event.listen(session, "do_orm_execute", lambda state: request_session_executions.append(threading.get_ident()))
    opened = _record_read_sessions(monkeypatch)
    studio = StudioFake()
    synchronizer = _composed(session, studio)
    loop_thread = threading.get_ident()

    result = await asyncio.to_thread(synchronizer.sync, review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert result.outcome is ProjectionSyncOutcome.CREATED
    assert request_session_executions == []
    assert len(opened) == 1
    record = opened[0]
    assert record["session"] is not session
    # Created, used and closed in the same worker thread -- never the event-loop thread.
    assert record["opened_in"] == record["closed_in"] != loop_thread
    assert _row(studio)["x_studio_review_status"] == "Decision Submitted"


def test_synchronizer_opens_and_closes_its_own_read_session_on_every_call(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept_vitel(session)
    opened = _record_read_sessions(monkeypatch)
    synchronizer = _composed(session, StudioFake())

    synchronizer.sync(review_id=REVIEW_ID, company_id=COMPANY_ID)
    synchronizer.plan(review_id=REVIEW_ID, company_id=COMPANY_ID)
    synchronizer.sync(review_id="review:missing", company_id=COMPANY_ID)  # fails inside the scope

    assert len(opened) == 3
    assert len({id(record["session"]) for record in opened}) == 3
    assert all(record["closed_in"] is not None for record in opened)


def test_every_sync_enters_and_exits_exactly_one_read_scope_even_on_failure() -> None:
    events: list[str] = []

    class ExplodingReviewReader:
        def get_review_item(self, query):
            raise RuntimeError("read failed")

    @contextmanager
    def read_scope():
        events.append("open")
        try:
            yield WorkbenchProjectionSources(
                review_reader=ExplodingReviewReader(),
                accepted_decision_reader=None,
                accepted_source_reader=None,
                execution_snapshot_reader=None,
                publisher=RaisingPublisher(),
            )
        finally:
            events.append("close")

    result = WorkbenchProjectionSynchronizer(read_scope=read_scope).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert result.outcome is ProjectionSyncOutcome.ERROR
    assert events == ["open", "close"]


def test_projection_reads_only_committed_hub_state(session: Session) -> None:
    _seed_committed(session, product_id=None, seller_item_code=VITEL_SKU, reasons=(PRODUCT_NOT_FOUND_REASON,))
    session.execute(WorkbenchReviewItem.__table__.update().values(invoice_number="UNCOMMITTED-EDIT"))
    session.flush()
    studio = StudioFake()
    synchronizer = _composed(session, studio)

    synchronizer.sync(review_id=REVIEW_ID, company_id=COMPANY_ID)
    assert _row(studio)["x_studio_invoice_number"] == "INV-19F"

    session.commit()
    result = synchronizer.sync(review_id=REVIEW_ID, company_id=COMPANY_ID)
    assert result.outcome is ProjectionSyncOutcome.UPDATED
    assert _row(studio)["x_studio_invoice_number"] == "UNCOMMITTED-EDIT"


def test_projection_failure_cannot_poison_or_roll_back_the_business_transaction(session: Session) -> None:
    from app.erp.exceptions import ErpRepositoryError

    class UnreachableStudio(StudioFake):
        def search_read(self, **kwargs):
            raise ErpRepositoryError("Odoo returned HTTP 503.")

    _accept_vitel(session)
    # Business work still in progress in the request session when a projection fails.
    session.execute(WorkbenchReviewItem.__table__.update().values(invoice_number="IN-FLIGHT-BUSINESS-WRITE"))
    session.flush()

    result = _composed(session, UnreachableStudio()).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert result.outcome is ProjectionSyncOutcome.ERROR
    assert session.is_active and session.in_transaction()
    session.commit()
    with Session(bind=session.get_bind()) as independent:
        item = independent.scalars(select(WorkbenchReviewItem)).one()
        assert item.invoice_number == "IN-FLIGHT-BUSINESS-WRITE"
        assert (item.status, item.version) == (ReviewStatus.DECISION_SUBMITTED.value, 2)
        assert independent.scalar(select(WorkbenchReviewDecision.decision_type)) == "select_workflow"


def test_repeated_composed_sync_is_idempotent(session: Session) -> None:
    _accept_vitel(session)
    studio = StudioFake()
    synchronizer = _composed(session, studio)
    first = synchronizer.sync(review_id=REVIEW_ID, company_id=COMPANY_ID)
    writes = studio.write_count

    second = synchronizer.sync(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert first.outcome is ProjectionSyncOutcome.CREATED
    assert second.outcome is ProjectionSyncOutcome.NO_CHANGE
    assert studio.write_count == writes


def test_synchronous_decision_path_projects_without_touching_the_request_session_after_commit(
    session: Session,
) -> None:
    from sqlalchemy import event

    _seed_committed(session, product_id=None, seller_item_code=VITEL_SKU, reasons=(PRODUCT_NOT_FOUND_REASON,))
    committed = {"done": False}
    executions_after_commit: list[bool] = []
    event.listen(session, "after_commit", lambda db: committed.__setitem__("done", True))
    event.listen(session, "do_orm_execute", lambda state: executions_after_commit.append(committed["done"]))
    studio = StudioFake()

    acknowledgement = SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        selected_product_reader=_Products(),
        selected_account_reader=_Accounts(),
        projection_synchronizer=_composed(session, studio),
    ).execute(_command((LineResolution(line_number="1", selected_product_id=VITEL_PRODUCT_ID),)))

    assert acknowledgement.accepted is True and acknowledgement.warnings == ()
    assert committed["done"] is True
    assert True not in executions_after_commit
    assert _row(studio)["x_studio_review_status"] == "Decision Submitted"
