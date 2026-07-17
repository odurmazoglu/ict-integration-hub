from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.exceptions import ConnectorError
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.db.base import Base
from app.models.uyumsoft_invoice import UyumsoftInvoiceMetadata
from app.models.uyumsoft_sync_run import UyumsoftSyncRun
from app.schemas.uyumsoft_invoices import (
    UyumsoftInvoiceListRequest,
    UyumsoftInvoiceListResponse,
    UyumsoftInvoiceSummary,
)
from app.services import uyumsoft_invoice_sync
from app.services.invoice_persistence import InvoicePersistenceService
from app.services.uyumsoft_invoice_sync import (
    MAX_SYNC_PAGES,
    SyncRunRepository,
    UyumsoftInvoiceSyncRequest,
    UyumsoftInvoiceSyncWorkflow,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


class RecordingUyumsoftClient(UyumsoftSoapClient):
    def __init__(
        self,
        *,
        inbox_pages: dict[int, list[UyumsoftInvoiceSummary]] | None = None,
        outbox_pages: dict[int, list[UyumsoftInvoiceSummary]] | None = None,
        fail_direction: str | None = None,
        fail_page: int = 1,
    ) -> None:
        self.inbox_pages = inbox_pages or {1: [_invoice("Inbox", "inbox-ettn-1")]}
        self.outbox_pages = outbox_pages or {1: [_invoice("Outbox", "outbox-ettn-1")]}
        self.fail_direction = fail_direction
        self.fail_page = fail_page
        self.calls: list[tuple[str, int]] = []

    def list_inbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        return self._list("Inbox", request, self.inbox_pages)

    def list_outbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        return self._list("Outbox", request, self.outbox_pages)

    def _list(
        self,
        direction: str,
        request: UyumsoftInvoiceListRequest,
        pages: dict[int, list[UyumsoftInvoiceSummary]],
    ) -> UyumsoftInvoiceListResponse:
        self.calls.append((direction, request.page))
        if self.fail_direction == direction and self.fail_page == request.page:
            raise ConnectorError(f"{direction} page {request.page} failed")
        invoices = pages.get(request.page, [])
        return UyumsoftInvoiceListResponse(
            direction=direction,
            page=request.page,
            page_size=request.page_size,
            total_count=sum(len(items) for items in pages.values()),
            invoices=invoices,
        )

    def __getattribute__(self, name: str) -> Any:
        forbidden = {"SetInvoicesTaken", "SendInvoice", "CancelInvoice", "RetrySendInvoices", "MoveToDraftStatus"}
        if name in forbidden:
            raise AssertionError(f"Forbidden operation accessed: {name}")
        return super().__getattribute__(name)


def test_first_run_tracks_completed_sync_run(session: Session) -> None:
    result = _workflow(session, RecordingUyumsoftClient()).run(_request())

    records = session.scalars(select(UyumsoftInvoiceMetadata)).all()
    sync_run = session.scalar(select(UyumsoftSyncRun))
    assert result.status == "completed"
    assert result.created == 2
    assert result.run_id is not None
    assert len(records) == 2
    assert sync_run is not None
    assert sync_run.status == "completed"
    assert sync_run.pages_fetched == 2
    assert sync_run.invoices_seen == 2
    assert sync_run.created_count == 2
    assert sync_run.summary["status"] == "completed"


def test_repeated_run_does_not_create_duplicates(session: Session) -> None:
    workflow = _workflow(session, RecordingUyumsoftClient())
    workflow.run(_request())
    second = workflow.run(_request())

    records = session.scalars(select(UyumsoftInvoiceMetadata)).all()
    runs = session.scalars(select(UyumsoftSyncRun)).all()
    assert len(records) == 2
    assert len(runs) == 2
    assert second.created == 0
    assert second.skipped == 2


def test_inbox_only_sync(session: Session) -> None:
    client = RecordingUyumsoftClient()
    result = _workflow(session, client).run(_request(directions=("Inbox",)))

    records = session.scalars(select(UyumsoftInvoiceMetadata)).all()
    assert client.calls == [("Inbox", 1)]
    assert result.created == 1
    assert {record.direction for record in records} == {"Inbox"}


def test_outbox_only_sync(session: Session) -> None:
    client = RecordingUyumsoftClient()
    result = _workflow(session, client).run(_request(directions=("Outbox",)))

    records = session.scalars(select(UyumsoftInvoiceMetadata)).all()
    assert client.calls == [("Outbox", 1)]
    assert result.created == 1
    assert {record.direction for record in records} == {"Outbox"}


def test_both_directions_sync(session: Session) -> None:
    client = RecordingUyumsoftClient()
    result = _workflow(session, client).run(_request())

    assert client.calls == [("Inbox", 1), ("Outbox", 1)]
    assert [summary.direction for summary in result.directions] == ["Inbox", "Outbox"]


def test_partial_failure_records_failed_run_and_preserves_progress(session: Session) -> None:
    client = RecordingUyumsoftClient(fail_direction="Outbox")
    workflow = _workflow(session, client)

    with pytest.raises(ConnectorError, match="Outbox page 1 failed"):
        workflow.run(_request())

    sync_run = session.scalar(select(UyumsoftSyncRun))
    records = session.scalars(select(UyumsoftInvoiceMetadata)).all()
    assert sync_run is not None
    assert sync_run.status == "failed"
    assert sync_run.pages_fetched == 1
    assert sync_run.invoices_seen == 1
    assert sync_run.created_count == 1
    assert sync_run.failure_message == "Outbox page 1 failed"
    assert sync_run.cursor_state["Inbox"]["status"] == "completed"
    assert sync_run.cursor_state["Outbox"]["status"] == "failed"
    assert {record.direction for record in records} == {"Inbox"}


def test_retry_safe_behavior_after_partial_failure(session: Session) -> None:
    with pytest.raises(ConnectorError):
        _workflow(session, RecordingUyumsoftClient(fail_direction="Outbox")).run(_request())

    retry = _workflow(session, RecordingUyumsoftClient()).run(_request())

    records = session.scalars(select(UyumsoftInvoiceMetadata)).all()
    assert len(records) == 2
    assert retry.created == 1
    assert retry.skipped == 1


def test_cursor_page_progress_for_multi_page_sync(session: Session) -> None:
    client = RecordingUyumsoftClient(
        inbox_pages={
            1: [_invoice("Inbox", "inbox-ettn-1")],
            2: [_invoice("Inbox", "inbox-ettn-2")],
        },
        outbox_pages={},
    )
    result = _workflow(session, client).run(_request(directions=("Inbox",), page_size=1, max_pages=5))

    sync_run = session.scalar(select(UyumsoftSyncRun))
    assert result.directions[0].pages_fetched == 2
    assert result.cursor_state["Inbox"]["current_page"] == 2
    assert sync_run is not None
    assert sync_run.cursor_state["Inbox"]["current_page"] == 2
    assert sync_run.cursor_state["Inbox"]["invoices_seen"] == 2


def test_bounded_configuration_validation(session: Session) -> None:
    workflow = _workflow(session, RecordingUyumsoftClient())
    with pytest.raises(ValueError, match="31 days"):
        workflow.run(_request(to_date=datetime(2026, 9, 1, tzinfo=UTC)))
    with pytest.raises(ValueError, match="page_size"):
        workflow.run(_request(page_size=101))
    with pytest.raises(ValueError, match="max_pages"):
        workflow.run(_request(max_pages=MAX_SYNC_PAGES + 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        workflow.run(
            UyumsoftInvoiceSyncRequest(
                from_date=datetime(2026, 7, 16),
                to_date=datetime(2026, 7, 17),
            )
        )


def test_read_only_operation_enforcement(session: Session) -> None:
    client = RecordingUyumsoftClient()
    _workflow(session, client).run(_request())

    assert client.calls == [("Inbox", 1), ("Outbox", 1)]


def test_sync_workflow_logs_only_safe_summary(monkeypatch: pytest.MonkeyPatch, session: Session) -> None:
    log_calls: list[dict[str, Any]] = []

    def capture_log(message: str, *args: Any, **kwargs: Any) -> None:
        log_calls.append({"message": message, **kwargs})

    monkeypatch.setattr(uyumsoft_invoice_sync.logger, "info", capture_log)
    _workflow(session, RecordingUyumsoftClient()).run(_request())

    assert log_calls
    assert log_calls[0]["message"] == "uyumsoft_invoice_sync_completed"
    assert log_calls[0]["extra"] == {
        "provider": "uyumsoft",
        "run_id": 1,
        "status": "completed",
        "created": 2,
        "updated": 0,
        "skipped": 0,
        "directions": ["Inbox", "Outbox"],
    }
    assert "inbox-ettn" not in str(log_calls)
    assert "secret" not in str(log_calls)


def _workflow(session: Session, client: RecordingUyumsoftClient) -> UyumsoftInvoiceSyncWorkflow:
    return UyumsoftInvoiceSyncWorkflow(
        client=client,
        persistence=InvoicePersistenceService(session),
        run_repository=SyncRunRepository(session),
    )


def _request(
    *,
    directions: tuple[str, ...] = ("Inbox", "Outbox"),
    from_date: datetime = datetime(2026, 7, 16, tzinfo=UTC),
    to_date: datetime = datetime(2026, 7, 17, tzinfo=UTC),
    page_size: int = 10,
    max_pages: int = 1,
) -> UyumsoftInvoiceSyncRequest:
    return UyumsoftInvoiceSyncRequest(
        from_date=from_date,
        to_date=to_date,
        directions=directions,
        page_size=page_size,
        max_pages=max_pages,
    )


def _invoice(direction: str, ettn: str) -> UyumsoftInvoiceSummary:
    return UyumsoftInvoiceSummary(
        invoice_id=f"{direction.lower()}-{ettn}",
        ettn=ettn,
        invoice_number=f"{direction}-INV-{ettn}",
        invoice_date=datetime(2026, 7, 17, tzinfo=UTC),
        sender="Sender",
        receiver="Receiver",
        tax_number="1234567890",
        currency="TRY",
        total_amount=Decimal("10.00"),
        direction=direction,
        status="NEW",
        extra_fields={"SecretToken": "secret"},
    )
