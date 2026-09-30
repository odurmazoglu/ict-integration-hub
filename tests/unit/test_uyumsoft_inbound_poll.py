"""Hub-owned Uyumsoft inbound poller.

Runs the real sync workflow, metadata persistence, document service, canonical
importer and ``ImportInvoiceUseCase`` (with the real SQLAlchemy import history and
review repository) against SQLite. Only Uyumsoft, the company lookup, the decision
engine and the Workbench projection synchronizer are faked.
"""

from __future__ import annotations

import ast
import io
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

import app.api.dependencies as api_dependencies
import app.composition.uyumsoft_inbound_poll as poll_composition
from app.application.use_cases.import_invoice import ImportInvoiceUseCase
from app.application.workbench import ReviewItemCreationService, ReviewStatus
from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.core.config import Settings
from app.db.base import Base
from app.domain.invoice import InternalInvoice
from app.domain.invoice.parser import parse_ubl_invoice
from app.erp.models import Company
from app.models.import_receipt import ImportReceipt
from app.models.invoice_document import InvoiceDocument
from app.models.uyumsoft_invoice import UyumsoftInvoiceMetadata
from app.models.uyumsoft_sync_run import UyumsoftSyncRun
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workflow_execution import WorkflowExecution
from app.persistence import SqlAlchemyImportHistory, SqlAlchemyReviewRepository, SqlAlchemyUnitOfWork
from app.schemas.uyumsoft_invoices import (
    UyumsoftInvoiceDocument,
    UyumsoftInvoiceListRequest,
    UyumsoftInvoiceListResponse,
    UyumsoftInvoiceSummary,
)
from app.services.document_service import InvoiceDocumentService
from app.services.document_storage import LocalDocumentStorage
from app.services.uyumsoft_canonical_import import (
    ExactCompanyResolver,
    UyumsoftCanonicalInvoiceImporter,
    import_idempotency_key,
)
from app.services.uyumsoft_inbound_poll import (
    POLL_STATUS_COMPLETED,
    POLL_STATUS_COMPLETED_WITH_ERRORS,
    POLL_STATUS_FAILED,
    POLL_STATUS_SKIPPED_LOCKED,
    PREVIEW_ALREADY_KNOWN,
    PREVIEW_NEW,
    PREVIEW_WOULD_IMPORT,
    InboundPollConfig,
    InProcessPollLock,
    KnownInboundInvoiceChecker,
    UyumsoftInboundPollCycle,
    UyumsoftInboundPollPreview,
)
from app.services.uyumsoft_invoice_sync import UyumsoftInvoiceSyncRequest, UyumsoftInvoiceSyncWorkflow
from app.workers import uyumsoft_inbound_poller
from app.workers.uyumsoft_inbound_poller import InboundPollScheduler
from tests.unit.test_import_invoice_use_case import FakeDecisionEngine, _review_required_decision
from tests.unit.test_ops_ui_01a_workbench_projection_sync import RecordingSynchronizer
from tests.unit.test_uyumsoft_canonical_import import FakeCompanyRepository

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ubl"
TEMPLATE_ETTN = b"11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
COMPANY_ID = 7
REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- fakes / harness


def _ettn(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def _ubl(number: int) -> bytes:
    return (FIXTURES / "valid_invoice.xml").read_bytes().replace(TEMPLATE_ETTN, _ettn(number).encode())


def _invoice_number(number: int) -> str:
    # InvoiceDocumentService downloads by invoice number (provider id only as a fallback).
    return f"SYN2026{number:06d}"


def _summary(number: int, *, direction: str = "Inbox") -> UyumsoftInvoiceSummary:
    return UyumsoftInvoiceSummary(
        invoice_id=f"provider-{number}",
        ettn=_ettn(number),
        invoice_number=_invoice_number(number),
        invoice_date=NOW - timedelta(days=1),
        sender="Synthetic Supplier Ltd",
        receiver="Synthetic Customer A.S.",
        tax_number="1111111111",
        currency="TRY",
        total_amount=Decimal("281.75"),
        direction=direction,  # type: ignore[arg-type]
        status="NEW",
    )


class FakeUyumsoft(UyumsoftSoapClient):
    """Read-only Uyumsoft double; state-changing operations and Outbox listing are forbidden."""

    def __init__(
        self,
        numbers: list[int],
        *,
        documents: dict[int, bytes] | None = None,
        list_error: Exception | None = None,
        on_list: Callable[[], None] | None = None,
    ) -> None:
        self.invoices = [_summary(number) for number in numbers]
        self.documents = {_invoice_number(number): _ubl(number) for number in numbers}
        for number, content in (documents or {}).items():
            self.documents[_invoice_number(number)] = content
        self.list_error = list_error
        self.on_list = on_list
        self.list_requests: list[UyumsoftInvoiceListRequest] = []
        self.download_calls: list[str] = []

    def list_inbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        self.list_requests.append(request)
        if self.on_list is not None:
            self.on_list()
        if self.list_error is not None:
            raise self.list_error
        start = (request.page - 1) * request.page_size
        return UyumsoftInvoiceListResponse(
            direction="Inbox",
            page=request.page,
            page_size=request.page_size,
            total_count=len(self.invoices),
            invoices=self.invoices[start : start + request.page_size],
        )

    def list_outbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        raise AssertionError("The inbound poller must never list outgoing invoices.")

    def download_invoice(self, *, direction: str, invoice_id: str) -> UyumsoftInvoiceDocument:
        self.download_calls.append(invoice_id)
        return UyumsoftInvoiceDocument(direction=direction, invoice_id=invoice_id, content=self.documents[invoice_id])

    def __getattribute__(self, name: str) -> Any:
        forbidden = {"SetInvoicesTaken", "SendInvoice", "CancelInvoice", "RetrySendInvoices", "MoveToDraftStatus"}
        if name in forbidden:
            raise AssertionError(f"Forbidden operation accessed: {name}")
        return super().__getattribute__(name)


class DenyingLock:
    def __init__(self) -> None:
        self.holds = 0

    def hold(self) -> Any:
        from contextlib import nullcontext

        self.holds += 1
        return nullcontext(False)


@dataclass
class Harness:
    engine: Engine
    storage_root: Path
    synchronizer: RecordingSynchronizer = field(default_factory=RecordingSynchronizer)
    use_cases_built: list[ImportInvoiceUseCase] = field(default_factory=list)

    def cycle(
        self,
        client: FakeUyumsoft,
        *,
        lock: Any = None,
        parse_invoice: Callable[[bytes], InternalInvoice] = parse_ubl_invoice,
        config: InboundPollConfig | None = None,
    ) -> UyumsoftInboundPollCycle:
        def importer_factory(session: Any) -> UyumsoftCanonicalInvoiceImporter:
            storage = LocalDocumentStorage(self.storage_root)

            def use_case_factory() -> ImportInvoiceUseCase:
                use_case = ImportInvoiceUseCase(
                    import_history=SqlAlchemyImportHistory(session),
                    decision_engine=FakeDecisionEngine(_review_required_decision()),
                    review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(session)),
                    unit_of_work=SqlAlchemyUnitOfWork(session),
                    workbench_projection_synchronizer=self.synchronizer,
                )
                self.use_cases_built.append(use_case)
                return use_case

            return UyumsoftCanonicalInvoiceImporter(
                document_service=InvoiceDocumentService(session=session, client=client, storage=storage),
                storage=storage,
                company_resolver=ExactCompanyResolver(
                    FakeCompanyRepository((Company(id=COMPANY_ID, name="ICT", tax_number="2222222222"),))
                ),
                import_use_case_factory=use_case_factory,
                parse_invoice=parse_invoice,
            )

        return UyumsoftInboundPollCycle(
            session_factory=sessionmaker(bind=self.engine),
            client=client,
            importer_factory=importer_factory,
            lock=lock or InProcessPollLock(),
            config=config or InboundPollConfig(),
            clock=lambda: NOW,
        )

    def preview(self, client: FakeUyumsoft) -> UyumsoftInboundPollPreview:
        @contextmanager
        def read_scope() -> Any:
            with sessionmaker(bind=self.engine)() as session:
                yield session
                assert not (session.new or session.dirty or session.deleted), "preview must not write"
                session.rollback()

        return UyumsoftInboundPollPreview(
            read_session_scope=read_scope, client=client, config=InboundPollConfig(), clock=lambda: NOW
        )

    def count(self, model: type[Base]) -> int:
        with sessionmaker(bind=self.engine)() as session:
            return int(session.scalar(select(func.count()).select_from(model)) or 0)

    def reviews(self) -> list[WorkbenchReviewItem]:
        with sessionmaker(bind=self.engine)() as session:
            return list(session.scalars(select(WorkbenchReviewItem).order_by(WorkbenchReviewItem.invoice_id)))


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    engine = create_engine(f"sqlite:///{tmp_path / 'hub.db'}")
    Base.metadata.create_all(engine)
    try:
        yield Harness(engine=engine, storage_root=tmp_path / "documents")
    finally:
        engine.dispose()


@pytest.fixture
def isolated_settings_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APP_ENV_FILE", str(tmp_path / "absent.env"))
    for name in (
        "UYUMSOFT_INBOUND_POLL_ENABLED",
        "UYUMSOFT_INBOUND_POLL_INTERVAL_SECONDS",
        "UYUMSOFT_INBOUND_POLL_LOOKBACK_DAYS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(uyumsoft_inbound_poller, "_install_signal_handlers", lambda stop: None)
    monkeypatch.setattr(uyumsoft_inbound_poller, "configure_logging", lambda settings: None)


class OneShotEvent(threading.Event):
    """Records the scheduler's wait timeout, then stops the loop."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        self.set()
        return True


# --------------------------------------------------------------------------- 1-3. configuration / scheduling


def test_disabled_by_default_and_production_interval_is_180_seconds(isolated_settings_env: None) -> None:
    settings = Settings()

    assert settings.uyumsoft_inbound_poll_enabled is False
    assert settings.uyumsoft_inbound_poll_interval_seconds == 180
    assert settings.uyumsoft_inbound_poll_lookback_days == 10


def test_interval_is_read_from_environment_and_bounded(
    isolated_settings_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UYUMSOFT_INBOUND_POLL_ENABLED", "true")
    monkeypatch.setenv("UYUMSOFT_INBOUND_POLL_INTERVAL_SECONDS", "180")
    assert Settings().uyumsoft_inbound_poll_interval_seconds == 180
    assert Settings().uyumsoft_inbound_poll_enabled is True

    monkeypatch.setenv("UYUMSOFT_INBOUND_POLL_INTERVAL_SECONDS", "5")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("argv", [["--once"], []])
def test_disabled_poller_makes_no_uyumsoft_call_and_builds_nothing(
    isolated_settings_env: None, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    def explode(*args: object, **kwargs: object) -> Any:
        raise AssertionError("A disabled poller must not build a client or a cycle.")

    monkeypatch.setattr(UyumsoftSoapClient, "from_settings", classmethod(explode))
    stop = threading.Event()
    stop.set()  # the non --once path idles on this event until stopped

    result = uyumsoft_inbound_poller.main(
        argv, settings=Settings(uyumsoft_inbound_poll_enabled=False), stop_event=stop, cycle_builder=explode
    )

    assert result == 0


def test_enabled_poller_runs_a_cycle_then_waits_the_configured_interval(isolated_settings_env: None) -> None:
    cycles: list[str] = []
    stop = OneShotEvent()

    result = uyumsoft_inbound_poller.main(
        [],
        settings=Settings(uyumsoft_inbound_poll_enabled=True, uyumsoft_inbound_poll_interval_seconds=180),
        stop_event=stop,
        cycle_builder=lambda settings: lambda: cycles.append("cycle"),
    )

    assert result == 0
    assert cycles == ["cycle"]
    assert len(stop.waits) == 1
    assert 179 < (stop.waits[0] or 0) <= 180


def test_long_cycle_never_overlaps_the_next_one() -> None:
    clock = iter([0.0, 250.0, 250.0, 260.0])  # first cycle takes 250s > 180s interval
    active = 0
    max_active = 0
    waits: list[float] = []

    class Stop(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            waits.append(timeout or 0)
            return False

    def cycle() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        active -= 1

    scheduler = InboundPollScheduler(
        cycle=cycle, interval_seconds=180, stop_event=Stop(), monotonic=lambda: next(clock)
    )
    assert scheduler.run(max_cycles=2) == 2
    assert max_active == 1
    assert waits == [0.0]  # the overrunning cycle is followed immediately, never concurrently


def test_scheduler_survives_a_crashing_cycle() -> None:
    calls: list[int] = []

    def cycle() -> None:
        calls.append(1)
        raise RuntimeError("boom")

    stop = threading.Event()
    scheduler = InboundPollScheduler(cycle=cycle, interval_seconds=60, stop_event=stop, monotonic=lambda: 0.0)
    stop.wait = lambda timeout=None: False  # type: ignore[method-assign]

    assert scheduler.run(max_cycles=3) == 3
    assert len(calls) == 3


# --------------------------------------------------------------------------- query / lookback


def test_cycle_queries_only_inbox_over_a_bounded_lookback_window(harness: Harness) -> None:
    client = FakeUyumsoft([1])

    result = harness.cycle(client).run()

    assert result.status == POLL_STATUS_COMPLETED
    [request] = client.list_requests
    assert request.from_date == NOW - timedelta(days=10)
    assert request.to_date == NOW + timedelta(days=1)
    assert request.page_size == 100
    assert request.date_field == "execution"
    assert request.only_newest_invoices is False  # never relies on provider "taken" state


def test_late_arriving_invoice_inside_lookback_is_still_imported(harness: Harness) -> None:
    harness.cycle(FakeUyumsoft([1])).run()
    late = FakeUyumsoft([1, 2])  # invoice 2 dated before invoice 1's poll, delivered later

    result = harness.cycle(late).run()

    assert (result.discovered, result.already_known, result.review_created) == (2, 1, 1)
    assert late.download_calls == [_invoice_number(2)]


# --------------------------------------------------------------------------- 4, 10, 11. import + idempotency


def test_new_invoice_goes_through_import_use_case_and_runtime_projection_sync(harness: Harness) -> None:
    result = harness.cycle(FakeUyumsoft([1])).run()

    [review] = harness.reviews()
    assert result.status == POLL_STATUS_COMPLETED
    assert (result.discovered, result.already_known, result.imported, result.review_created) == (1, 0, 1, 1)
    assert len(harness.use_cases_built) == 1
    assert review.idempotency_key == f"uyumsoft:company:{COMPANY_ID}:inbox:ettn:{_ettn(1)}"
    assert review.status == ReviewStatus.PENDING_REVIEW.value
    # Post-commit projection went through the runtime synchronizer ImportInvoiceUseCase owns.
    assert harness.synchronizer.calls == [(review.review_id, COMPANY_ID)]


def test_second_cycle_skips_known_invoice_without_download_or_duplicate(harness: Harness) -> None:
    client = FakeUyumsoft([1])
    harness.cycle(client).run()

    second = harness.cycle(client).run()

    assert second.status == POLL_STATUS_COMPLETED
    assert (second.discovered, second.already_known, second.imported, second.failed) == (1, 1, 0, 0)
    assert client.download_calls == [_invoice_number(1)]
    assert harness.count(WorkbenchReviewItem) == 1
    assert harness.count(UyumsoftInvoiceMetadata) == 1
    assert harness.count(InvoiceDocument) == 1
    assert len(harness.synchronizer.calls) == 1
    # The empty second cycle is a log line only; the importing first cycle kept its row.
    assert second.audit_recorded is False and second.run_id is None
    assert harness.count(UyumsoftSyncRun) == 1


def test_restart_does_not_duplicate_previously_ingested_invoices(harness: Harness) -> None:
    harness.cycle(FakeUyumsoft([1, 2])).run()

    # A brand-new process: new client, new cycle, new sessions -- only the database persists.
    fresh = FakeUyumsoft([1, 2])
    result = harness.cycle(fresh).run()

    assert (result.discovered, result.already_known, result.imported) == (2, 2, 0)
    assert fresh.download_calls == []
    assert harness.count(WorkbenchReviewItem) == 2


def test_stale_known_check_is_still_caught_by_import_use_case_idempotency(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same invoice discovered concurrently: the pre-check missed it, the use case must not."""

    harness.cycle(FakeUyumsoft([1])).run()
    monkeypatch.setattr(KnownInboundInvoiceChecker, "is_known", lambda self, invoice: False)

    result = harness.cycle(FakeUyumsoft([1])).run()

    assert result.status == POLL_STATUS_COMPLETED
    assert (result.already_known, result.already_imported, result.imported) == (0, 1, 0)
    assert harness.count(WorkbenchReviewItem) == 1
    assert len(harness.synchronizer.calls) == 1


def test_known_checker_matches_exact_import_idempotency_key_only(harness: Harness) -> None:
    invoice = _summary(1)
    with sessionmaker(bind=harness.engine)() as session:
        checker = KnownInboundInvoiceChecker(session)
        assert checker.is_known(invoice) is False
        session.add(
            ImportReceipt(
                company_id=COMPANY_ID,
                idempotency_key=import_idempotency_key(company_id=COMPANY_ID, provider="uyumsoft", invoice=invoice),
                invoice_id=_ettn(1),
                status="dry_run",
            )
        )
        # Look-alikes that must not count: other direction, other ettn, LIKE wildcards.
        session.add(
            ImportReceipt(
                company_id=COMPANY_ID,
                idempotency_key=f"uyumsoft:company:{COMPANY_ID}:outbox:ettn:{_ettn(2)}",
                invoice_id="x",
                status="dry_run",
            )
        )
        session.flush()

        assert checker.is_known(invoice) is True
        assert checker.is_known(_summary(2)) is False
        assert checker.is_known(_summary(1, direction="Outbox")) is False
        wildcard = _summary(3).model_copy(update={"ettn": "0000000_-0000-4000-8000-%"})
        assert checker.is_known(wildcard) is False


# --------------------------------------------------------------------------- 6-7. locking


def test_cycle_skips_when_another_worker_holds_the_lock(harness: Harness) -> None:
    client = FakeUyumsoft([1])
    lock = DenyingLock()

    result = harness.cycle(client, lock=lock).run()

    assert result.status == POLL_STATUS_SKIPPED_LOCKED
    assert result.failure_type is None
    assert client.list_requests == []
    assert harness.count(UyumsoftSyncRun) == 0
    assert harness.count(WorkbenchReviewItem) == 0


def test_overlapping_cycle_in_same_process_is_skipped(harness: Harness) -> None:
    lock = InProcessPollLock()
    inner_results = []
    inner_client = FakeUyumsoft([2])

    def overlap() -> None:
        if not inner_results:
            inner_results.append(harness.cycle(inner_client, lock=lock).run())

    outer = harness.cycle(FakeUyumsoft([1], on_list=overlap), lock=lock).run()

    assert outer.status == POLL_STATUS_COMPLETED
    assert inner_results[0].status == POLL_STATUS_SKIPPED_LOCKED
    assert inner_client.list_requests == []
    assert harness.count(WorkbenchReviewItem) == 1


def test_lock_failure_is_a_failed_cycle_without_side_effects(harness: Harness) -> None:
    class BrokenLock:
        def hold(self) -> Any:
            raise ConnectionError("database unreachable")

    client = FakeUyumsoft([1])
    result = harness.cycle(client, lock=BrokenLock()).run()

    assert result.status == POLL_STATUS_FAILED
    assert result.failure_type == "ConnectionError"
    assert client.list_requests == []


def test_postgres_engine_gets_advisory_lock() -> None:
    from app.services.uyumsoft_inbound_poll import PostgresAdvisoryPollLock

    engine = create_engine("postgresql+psycopg://user:pw@localhost:1/none")
    try:
        assert isinstance(poll_composition.build_poll_lock(engine), PostgresAdvisoryPollLock)
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- 8-9. failure isolation


def test_one_bad_invoice_does_not_stop_the_rest(harness: Harness) -> None:
    def parse(content: bytes) -> InternalInvoice:
        if _ettn(3).encode() in content:
            raise RuntimeError("unexpected parser crash")
        return parse_ubl_invoice(content)

    client = FakeUyumsoft([1, 2, 3, 4], documents={2: b"<?xml version='1.0'?><NotAnInvoice/>"})

    result = harness.cycle(client, parse_invoice=parse).run()

    assert result.status == POLL_STATUS_COMPLETED_WITH_ERRORS
    assert (result.discovered, result.imported, result.failed) == (4, 2, 2)
    assert [review.invoice_id for review in harness.reviews()] == [_ettn(1), _ettn(4)]

    # Failed invoices are not "known": the next cycle retries exactly those two. The
    # transient crash recovers; the provider's bad document (same bytes) fails again.
    retry_client = FakeUyumsoft([1, 2, 3, 4], documents={2: b"<?xml version='1.0'?><NotAnInvoice/>"})
    retry = harness.cycle(retry_client).run()
    assert (retry.already_known, retry.imported, retry.failed) == (2, 1, 1)
    assert sorted(retry_client.download_calls) == [_invoice_number(2), _invoice_number(3)]
    assert harness.count(WorkbenchReviewItem) == 3


@pytest.mark.parametrize(
    "error", [ConnectorError("Uyumsoft unavailable."), ConnectorTimeoutError("Uyumsoft timed out.")]
)
def test_uyumsoft_outage_changes_no_business_state(harness: Harness, error: ConnectorError) -> None:
    client = FakeUyumsoft([1], list_error=error)

    result = harness.cycle(client).run()

    assert result.status == POLL_STATUS_FAILED
    assert result.failure_type == type(error).__name__
    assert harness.count(UyumsoftInvoiceMetadata) == 0
    assert harness.count(InvoiceDocument) == 0
    assert harness.count(WorkbenchReviewItem) == 0
    assert harness.count(ImportReceipt) == 0
    assert harness.synchronizer.calls == []  # no Odoo projection either
    with sessionmaker(bind=harness.engine)() as session:
        [run] = session.scalars(select(UyumsoftSyncRun)).all()
        assert run.status == "failed"  # technical audit row only

    # Next cycle simply retries.
    assert harness.cycle(FakeUyumsoft([1])).run().status == POLL_STATUS_COMPLETED


def test_unexpected_workflow_error_rolls_back_the_cycle(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_run(self: UyumsoftInvoiceSyncWorkflow, request: UyumsoftInvoiceSyncRequest) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr(UyumsoftInvoiceSyncWorkflow, "run", broken_run)

    result = harness.cycle(FakeUyumsoft([1])).run()

    assert result.status == POLL_STATUS_FAILED
    assert result.failure_message is None  # only safe messages are surfaced
    assert harness.count(UyumsoftSyncRun) == 0


# --------------------------------------------------------------------------- 12-13. boundaries


def test_polling_never_decides_or_executes(harness: Harness) -> None:
    harness.cycle(FakeUyumsoft([1, 2])).run()

    assert harness.count(WorkbenchReviewDecision) == 0
    assert harness.count(WorkflowExecution) == 0
    assert {review.status for review in harness.reviews()} == {ReviewStatus.PENDING_REVIEW.value}


@pytest.mark.parametrize(
    "module",
    ["app/services/uyumsoft_inbound_poll.py", "app/workers/uyumsoft_inbound_poller.py"],
)
def test_poller_modules_have_no_odoo_or_execution_dependency(module: str) -> None:
    tree = ast.parse((REPO_ROOT / module).read_text())
    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }

    forbidden = ("odoo", "execution", "billing", "decision", "write_authorization")
    assert not [name for name in imported if any(marker in name for marker in forbidden)]


def test_poller_composes_the_same_importer_as_the_manual_sync_route(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        poll_composition.build_uyumsoft_canonical_invoice_importer
        is api_dependencies.build_uyumsoft_canonical_invoice_importer
    )
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        poll_composition,
        "build_uyumsoft_canonical_invoice_importer",
        lambda **kwargs: calls.append(kwargs) or "importer",
    )
    engine = create_engine("sqlite://")
    client = FakeUyumsoft([])
    settings = Settings(uyumsoft_inbound_poll_enabled=True)

    cycle = poll_composition.build_uyumsoft_inbound_poll_cycle(
        settings=settings,
        engine=engine,
        uyumsoft_client=client,
        storage=object(),  # type: ignore[arg-type]
    )
    with sessionmaker(bind=engine)() as session:
        assert cycle._importer_factory(session) == "importer"

    assert calls[0]["session"] is session
    assert calls[0]["settings"] is settings
    assert calls[0]["uyumsoft_client"] is client
    engine.dispose()


# --------------------------------------------------------------------------- sync-run audit volume


def test_meaningful_cycles_keep_their_audit_row_and_empty_ones_do_not(harness: Harness) -> None:
    empty = harness.cycle(FakeUyumsoft([])).run()
    importing = harness.cycle(FakeUyumsoft([1])).run()
    all_known = harness.cycle(FakeUyumsoft([1])).run()
    failing = harness.cycle(FakeUyumsoft([1, 2], documents={2: b"<?xml version='1.0'?><NotAnInvoice/>"})).run()

    assert (empty.status, empty.audit_recorded, empty.run_id) == (POLL_STATUS_COMPLETED, False, None)
    assert (all_known.audit_recorded, all_known.already_known) == (False, 1)
    assert importing.audit_recorded is True and importing.run_id is not None
    assert failing.status == POLL_STATUS_COMPLETED_WITH_ERRORS and failing.audit_recorded is True
    with sessionmaker(bind=harness.engine)() as session:
        runs = session.scalars(select(UyumsoftSyncRun).order_by(UyumsoftSyncRun.id)).all()
        assert [run.id for run in runs] == [importing.run_id, failing.run_id]
        assert [run.status for run in runs] == ["completed", "completed"]
        assert runs[1].summary["failed_import_count"] == 1


def test_empty_cycle_still_logs_its_counters(harness: Harness) -> None:
    import logging

    harness.cycle(FakeUyumsoft([1])).run()
    messages: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    poll_logger = logging.getLogger("app.services.uyumsoft_inbound_poll")
    # Another test may have run Alembic's fileConfig(), which disables existing loggers.
    handler, previous = Collect(level=logging.INFO), (poll_logger.level, poll_logger.disabled)
    poll_logger.addHandler(handler)
    poll_logger.setLevel(logging.INFO)
    poll_logger.disabled = False
    try:
        harness.cycle(FakeUyumsoft([1])).run()
    finally:
        poll_logger.removeHandler(handler)
        poll_logger.level, poll_logger.disabled = previous

    [finished] = [message for message in messages if "poll_finished" in message]
    assert "status=completed discovered=1 already_known=1 imported=0" in finished
    assert "audit_recorded=False" in finished


# --------------------------------------------------------------------------- first-run preview


def _seed_already_known_and_retry(harness: Harness) -> None:
    """Invoice 1 imported; invoice 2 seen by the Hub but failed (never imported)."""

    harness.cycle(FakeUyumsoft([1, 2], documents={2: b"<?xml version='1.0'?><NotAnInvoice/>"})).run()


def test_preview_classifies_every_invoice_without_writing_anything(harness: Harness) -> None:
    _seed_already_known_and_retry(harness)
    before = {model: harness.count(model) for model in (UyumsoftInvoiceMetadata, InvoiceDocument, UyumsoftSyncRun)}
    before_reviews = harness.count(WorkbenchReviewItem)
    client = FakeUyumsoft([1, 2, 3])

    preview = harness.preview(client).run()

    assert [(item.ettn, item.status) for item in preview.items] == [
        (_ettn(1), PREVIEW_ALREADY_KNOWN),
        (_ettn(2), PREVIEW_WOULD_IMPORT),
        (_ettn(3), PREVIEW_NEW),
    ]
    assert [item.ettn for item in preview.would_import] == [_ettn(2), _ettn(3)]
    assert client.download_calls == []
    assert {model: harness.count(model) for model in before} == before
    assert harness.count(WorkbenchReviewItem) == before_reviews
    assert harness.synchronizer.calls == [(harness.reviews()[0].review_id, COMPANY_ID)]  # only the seed import


def test_preview_uses_the_exact_window_and_paging_of_the_cycle(harness: Harness) -> None:
    preview_client = FakeUyumsoft(list(range(1, 151)))
    cycle_client = FakeUyumsoft(list(range(1, 151)))

    preview = harness.preview(preview_client).run()
    harness.cycle(cycle_client).run()

    assert preview_client.list_requests == cycle_client.list_requests
    assert (preview.from_date, preview.to_date, preview.pages_fetched) == (
        NOW - timedelta(days=10),
        NOW + timedelta(days=1),
        2,
    )


def test_preview_predicts_exactly_what_the_next_cycle_attempts(harness: Harness) -> None:
    _seed_already_known_and_retry(harness)
    bad_document = {2: b"<?xml version='1.0'?><NotAnInvoice/>"}

    preview = harness.preview(FakeUyumsoft([1, 2, 3], documents=bad_document)).run()
    cycle_client = FakeUyumsoft([1, 2, 3], documents=bad_document)
    result = harness.cycle(cycle_client).run()

    # Every WOULD_IMPORT/NEW invoice enters the import pipeline (and is downloaded);
    # every ALREADY_KNOWN one is skipped. Success still depends on the import itself.
    assert set(cycle_client.download_calls) == {_invoice_number(2), _invoice_number(3)}
    assert {item.ettn for item in preview.would_import} == {_ettn(2), _ettn(3)}
    assert result.already_known == preview.count(PREVIEW_ALREADY_KNOWN)
    assert (result.review_created, result.failed) == (1, 1)


def test_preview_cli_runs_while_polling_is_disabled_and_prints_the_list(
    isolated_settings_env: None, harness: Harness
) -> None:
    _seed_already_known_and_retry(harness)
    out = io.StringIO()

    exit_code = uyumsoft_inbound_poller.main(
        ["--preview"],
        settings=Settings(uyumsoft_inbound_poll_enabled=False),
        preview_builder=lambda settings: harness.preview(FakeUyumsoft([1, 2, 3])).run,
        cycle_builder=lambda settings: pytest.fail("preview must never build a poll cycle"),
        out=out,
    )

    text = out.getvalue()
    assert exit_code == 0
    assert "read-only" in text
    assert f"ALREADY_KNOWN\t{_ettn(1)}\t" in text
    assert f"WOULD_IMPORT\t{_ettn(2)}\t" in text
    assert f"NEW\t{_ettn(3)}\t" in text
    assert "summary discovered=3 already_known=1 new=1 would_import=1 next_cycle_would_import=2" in text
    assert "<Invoice" not in text


def test_preview_failure_is_reported_with_a_non_zero_exit(
    isolated_settings_env: None, harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeUyumsoft([1], list_error=ConnectorError("Uyumsoft unavailable."))

    exit_code = uyumsoft_inbound_poller.main(
        ["--preview"], settings=Settings(), preview_builder=lambda settings: harness.preview(client).run
    )

    assert exit_code == uyumsoft_inbound_poller.EXIT_PREVIEW_FAILED
    assert "Preview failed: ConnectorError" in capsys.readouterr().err
    assert harness.count(UyumsoftSyncRun) == 0


# --------------------------------------------------------------------------- Workbench projection chain


def test_projection_sync_runs_only_after_the_review_is_committed(harness: Harness) -> None:
    seen_committed: list[bool] = []

    class CommitCheckingSynchronizer(RecordingSynchronizer):
        def sync(self, *, review_id: str, company_id: int) -> Any:
            with sessionmaker(bind=harness.engine)() as other_connection:  # sees committed rows only
                seen_committed.append(
                    other_connection.scalar(
                        select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_id)
                    )
                    is not None
                )
            return super().sync(review_id=review_id, company_id=company_id)

    harness.synchronizer = CommitCheckingSynchronizer()

    harness.cycle(FakeUyumsoft([1])).run()

    assert seen_committed == [True]


@pytest.mark.parametrize("publish_enabled", [True, False])
def test_poller_import_uses_the_runtime_workbench_synchronizer_and_no_other_odoo_writer(
    monkeypatch: pytest.MonkeyPatch, publish_enabled: bool
) -> None:
    from app.application.workbench.projection_sync import WorkbenchProjectionSynchronizer

    for suffix, value in _PROJECTION_MAPPING_ENV.items():
        monkeypatch.setenv(f"ODOO_WORKBENCH_PUBLISHER_{suffix}", value)
    engine = create_engine("sqlite://")
    settings = Settings(odoo_workbench_projection_publish_enabled=publish_enabled)
    cycle = poll_composition.build_uyumsoft_inbound_poll_cycle(
        settings=settings,
        engine=engine,
        uyumsoft_client=FakeUyumsoft([]),
        storage=object(),  # type: ignore[arg-type]
    )

    with sessionmaker(bind=engine)() as session:
        use_case = cycle._importer_factory(session)._import_use_case_factory()

    assert isinstance(use_case, ImportInvoiceUseCase)
    assert use_case._workbench_projection_publisher is None  # the legacy direct publisher is never wired
    if publish_enabled:
        assert isinstance(use_case._workbench_projection_synchronizer, WorkbenchProjectionSynchronizer)
    else:
        assert use_case._workbench_projection_synchronizer is None
    engine.dispose()


_PROJECTION_MAPPING_ENV = {
    "PARENT_MODEL": "x_ipp_import_workbench",
    "NAME_FIELD": "x_name",
    "REVIEW_ID_FIELD": "x_studio_review_id",
    "COMPANY_ID_FIELD": "x_studio_company_id",
    "INVOICE_NUMBER_FIELD": "x_studio_invoice_number",
    "SUPPLIER_FIELD": "x_studio_supplier",
    "SUPPLIER_TAX_NUMBER_FIELD": "x_studio_supplier_tax_number",
    "INVOICE_DATE_FIELD": "x_studio_invoice_date",
    "CURRENCY_FIELD": "x_studio_currency",
    "INVOICE_TOTAL_FIELD": "x_studio_invoice_total",
    "REVIEW_STATUS_FIELD": "x_studio_review_status",
    "WORKFLOW_FIELD": "x_studio_workflow",
    "REVIEW_VERSION_FIELD": "x_studio_review_version",
    "LAST_SYNC_AT_FIELD": "x_studio_last_sync_at",
}
