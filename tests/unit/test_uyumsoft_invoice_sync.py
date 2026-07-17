from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.db.base import Base
from app.models.uyumsoft_invoice import UyumsoftInvoiceMetadata
from app.schemas.uyumsoft_invoices import (
    UyumsoftInvoiceListRequest,
    UyumsoftInvoiceListResponse,
    UyumsoftInvoiceSummary,
)
from app.services import uyumsoft_invoice_sync
from app.services.invoice_persistence import InvoicePersistenceService
from app.services.uyumsoft_invoice_sync import UyumsoftInvoiceSyncRequest, UyumsoftInvoiceSyncWorkflow


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


class RecordingUyumsoftClient(UyumsoftSoapClient):
    def __init__(self) -> None:
        pass

    def list_inbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        return _response("Inbox", request, "inbox-ettn")

    def list_outbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        return _response("Outbox", request, "outbox-ettn")

    def __getattribute__(self, name: str) -> Any:
        forbidden = {"SetInvoicesTaken", "SendInvoice", "CancelInvoice", "RetrySendInvoices", "MoveToDraftStatus"}
        if name in forbidden:
            raise AssertionError(f"Forbidden operation accessed: {name}")
        return super().__getattribute__(name)


def test_sync_workflow_persists_inbox_and_outbox(session: Session) -> None:
    result = UyumsoftInvoiceSyncWorkflow(
        client=RecordingUyumsoftClient(),
        persistence=InvoicePersistenceService(session),
    ).run(
        UyumsoftInvoiceSyncRequest(
            from_date=datetime(2026, 7, 16, tzinfo=UTC),
            to_date=datetime(2026, 7, 17, tzinfo=UTC),
            page_size=10,
            max_pages=1,
        )
    )

    records = session.scalars(select(UyumsoftInvoiceMetadata)).all()
    assert result.created == 2
    assert result.updated == 0
    assert result.skipped == 0
    assert {record.direction for record in records} == {"Inbox", "Outbox"}


def test_sync_workflow_repeated_run_skips_existing_records(session: Session) -> None:
    workflow = UyumsoftInvoiceSyncWorkflow(
        client=RecordingUyumsoftClient(),
        persistence=InvoicePersistenceService(session),
    )
    request = UyumsoftInvoiceSyncRequest(
        from_date=datetime(2026, 7, 16, tzinfo=UTC),
        to_date=datetime(2026, 7, 17, tzinfo=UTC),
        page_size=10,
        max_pages=1,
    )

    workflow.run(request)
    second = workflow.run(request)

    assert second.created == 0
    assert second.skipped == 2


def test_sync_workflow_logs_only_summary(monkeypatch: pytest.MonkeyPatch, session: Session) -> None:
    log_calls: list[dict[str, Any]] = []

    def capture_log(message: str, *args: Any, **kwargs: Any) -> None:
        log_calls.append({"message": message, **kwargs})

    monkeypatch.setattr(uyumsoft_invoice_sync.logger, "info", capture_log)
    workflow = UyumsoftInvoiceSyncWorkflow(
        client=RecordingUyumsoftClient(),
        persistence=InvoicePersistenceService(session),
    )

    workflow.run(
        UyumsoftInvoiceSyncRequest(
            from_date=datetime(2026, 7, 16, tzinfo=UTC),
            to_date=datetime(2026, 7, 17, tzinfo=UTC),
            page_size=10,
            max_pages=1,
        )
    )

    assert log_calls
    assert log_calls[0]["message"] == "uyumsoft_invoice_sync_completed"
    assert log_calls[0]["extra"] == {
        "provider": "uyumsoft",
        "created": 2,
        "updated": 0,
        "skipped": 0,
        "directions": ["Inbox", "Outbox"],
    }


def test_sync_workflow_rejects_naive_datetimes(session: Session) -> None:
    workflow = UyumsoftInvoiceSyncWorkflow(
        client=RecordingUyumsoftClient(),
        persistence=InvoicePersistenceService(session),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        workflow.run(
            UyumsoftInvoiceSyncRequest(
                from_date=datetime(2026, 7, 16),
                to_date=datetime(2026, 7, 17),
            )
        )


def _response(
    direction: str,
    request: UyumsoftInvoiceListRequest,
    ettn: str,
) -> UyumsoftInvoiceListResponse:
    return UyumsoftInvoiceListResponse(
        direction=direction,
        page=request.page,
        page_size=request.page_size,
        total_count=1,
        invoices=[
            UyumsoftInvoiceSummary(
                invoice_id=f"{direction.lower()}-1",
                ettn=ettn,
                invoice_number=f"{direction}-INV-1",
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
        ],
    )
