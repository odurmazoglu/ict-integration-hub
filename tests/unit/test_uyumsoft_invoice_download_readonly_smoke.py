import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.exceptions import ConnectorError
from app.core.config import Settings
from app.db.base import Base
from app.models.invoice_document import InvoiceDocument
from app.schemas.uyumsoft_invoices import (
    UyumsoftInvoiceDocument,
    UyumsoftInvoiceListRequest,
    UyumsoftInvoiceListResponse,
    UyumsoftInvoiceSummary,
)
from app.services.document_storage import LocalDocumentStorage
from scripts import uyumsoft_invoice_download_readonly_smoke as download_smoke

XML_CONTENT = b'<?xml version="1.0"?><Invoice><ID>INV-1</ID></Invoice>'


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


def test_live_download_readonly_gate_requires_strict_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(download_smoke.ENABLE_FLAG, raising=False)

    errors = download_smoke._live_download_readonly_errors(
        _settings(live_connector_readonly=False, uyumsoft_environment="test")
    )

    assert f"Live invoice download smoke is disabled. Set {download_smoke.ENABLE_FLAG}=1 to run it." in errors
    assert "Live invoice download smoke requires UYUMSOFT_ENVIRONMENT=production." in errors
    assert "Live invoice download smoke requires LIVE_CONNECTOR_READONLY=true." in errors


def test_successful_sanitized_download_result_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(download_smoke.ENABLE_FLAG, "1")
    client = RecordingDownloadSmokeClient()

    result = download_smoke.run_validation(
        settings=_settings(),
        client=client,
        session=session,
        storage=LocalDocumentStorage(tmp_path),
        selection=_selection(invoice_id="in-1"),
    )

    documents = session.scalars(select(InvoiceDocument)).all()
    assert result == {
        "success": True,
        "direction": "Inbox",
        "invoice_identifier_present": True,
        "document_id": documents[0].id,
        "storage_identifier_present": True,
        "filename": f"{sha256(XML_CONTENT).hexdigest()}.xml",
        "content_type": "application/xml",
        "byte_size": len(XML_CONTENT),
        "sha256": sha256(XML_CONTENT).hexdigest(),
        "newly_created": True,
        "reused": False,
        "idempotency": {
            "ok": True,
            "second_status": "existing",
            "duplicate_document_created": False,
        },
        "provider_mutation_attempted": False,
        "error": None,
    }
    assert len(documents) == 1
    assert client.calls == [("download_invoice", "Inbox", "in-1"), ("download_invoice", "Inbox", "in-1")]
    assert "<Invoice" not in download_smoke._json_dumps(result)


def test_page_size_one_lookup_selects_first_inbox_invoice(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(download_smoke.ENABLE_FLAG, "1")
    client = RecordingDownloadSmokeClient()

    result = download_smoke.run_validation(
        settings=_settings(),
        client=client,
        session=session,
        storage=LocalDocumentStorage(tmp_path),
        selection=_selection(invoice_id=None),
    )

    assert result["success"] is True
    assert client.requests[0].page == 1
    assert client.requests[0].page_size == 1
    assert client.requests[0].only_newest_invoices is False
    assert client.calls[0] == ("list_inbox_invoices",)


def test_provider_error_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(download_smoke.ENABLE_FLAG, "1")
    client = RecordingDownloadSmokeClient(fail_with=ConnectorError("<Invoice>secret payload</Invoice>"))

    result = download_smoke.run_validation(
        settings=_settings(),
        client=client,
        session=session,
        storage=LocalDocumentStorage(tmp_path),
        selection=_selection(invoice_id="in-1"),
    )

    output = download_smoke._json_dumps(result)
    assert result["success"] is False
    assert result["error"] == download_smoke.SAFE_FAILURE_MESSAGE
    assert "<Invoice" not in output
    assert "secret payload" not in output


def test_unwrapped_provider_exception_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(download_smoke.ENABLE_FLAG, "1")
    client = RecordingDownloadSmokeClient(fail_with=RuntimeError("low-level network failure with secret"))

    result = download_smoke.run_validation(
        settings=_settings(),
        client=client,
        session=session,
        storage=LocalDocumentStorage(tmp_path),
        selection=_selection(invoice_id="in-1"),
    )

    output = download_smoke._json_dumps(result)
    assert result["success"] is False
    assert result["error"] == download_smoke.SAFE_FAILURE_MESSAGE
    assert "low-level network failure" not in output
    assert "secret" not in output


def test_missing_invoice_identifier_does_not_download(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(download_smoke.ENABLE_FLAG, "1")
    client = RecordingDownloadSmokeClient(invoice_id=None)

    result = download_smoke.run_validation(
        settings=_settings(),
        client=client,
        session=session,
        storage=LocalDocumentStorage(tmp_path),
        selection=_selection(invoice_id=None),
    )

    assert result["success"] is False
    assert result["invoice_identifier_present"] is False
    assert result["error"] == download_smoke.MISSING_INVOICE_ID_MESSAGE
    assert client.calls == [("list_inbox_invoices",)]


def test_no_provider_mutation_calls(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(download_smoke.ENABLE_FLAG, "1")
    client = RecordingDownloadSmokeClient()

    download_smoke.run_validation(
        settings=_settings(),
        client=client,
        session=session,
        storage=LocalDocumentStorage(tmp_path),
        selection=_selection(invoice_id="in-1"),
    )

    assert client.calls == [("download_invoice", "Inbox", "in-1"), ("download_invoice", "Inbox", "in-1")]


def test_argument_validation_requires_explicit_id_or_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["uyumsoft_invoice_download_readonly_smoke.py"])

    with pytest.raises(SystemExit):
        download_smoke._parse_args()


def test_argument_validation_rejects_empty_invoice_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["uyumsoft_invoice_download_readonly_smoke.py", "--invoice-id", " "])

    with pytest.raises(SystemExit):
        download_smoke._parse_args()


class RecordingDownloadSmokeClient:
    def __init__(
        self,
        *,
        invoice_id: str | None = "in-1",
        fail_with: Exception | None = None,
    ) -> None:
        self.invoice_id = invoice_id
        self.fail_with = fail_with
        self.calls: list[tuple[Any, ...]] = []
        self.requests: list[UyumsoftInvoiceListRequest] = []

    def list_inbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        self.calls.append(("list_inbox_invoices",))
        self.requests.append(request)
        return UyumsoftInvoiceListResponse(
            direction="Inbox",
            page=request.page,
            page_size=request.page_size,
            total_count=1,
            invoices=[_summary(self.invoice_id)],
        )

    def download_invoice(self, *, direction: str, invoice_id: str) -> UyumsoftInvoiceDocument:
        self.calls.append(("download_invoice", direction, invoice_id))
        if self.fail_with is not None:
            raise self.fail_with
        return UyumsoftInvoiceDocument(direction=direction, invoice_id=invoice_id, content=XML_CONTENT)

    def __getattribute__(self, name: str) -> Any:
        forbidden = {
            "SetInvoicesTaken",
            "SendInvoice",
            "CancelInvoice",
            "RetrySendInvoices",
            "MoveToDraftStatus",
            "set_invoices_taken",
            "send_invoice",
            "cancel_invoice",
            "retry_send_invoices",
            "move_to_draft_status",
            "accept_invoice",
            "reject_invoice",
        }
        if name in forbidden:
            raise AssertionError(f"Forbidden operation accessed: {name}")
        return super().__getattribute__(name)


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "development",
        "live_connector_readonly": True,
        "production_operations_enabled": False,
        "production_approval_ack": "",
        "uyumsoft_environment": "production",
        "uyumsoft_username": "live-user",
        "uyumsoft_password": SecretStr("live-password"),
        "odoo_database": "staging",
        "odoo_api_key": SecretStr("staging-api-key"),
    }
    values.update(overrides)
    return Settings(**values)


def _selection(invoice_id: str | None) -> download_smoke.DownloadSmokeSelection:
    return download_smoke.DownloadSmokeSelection(
        direction="inbox",
        invoice_id=invoice_id,
        from_date=datetime(2026, 7, 20, tzinfo=UTC),
        to_date=datetime(2026, 7, 21, tzinfo=UTC),
        date_field="execution",
    )


def _summary(invoice_id: str | None) -> UyumsoftInvoiceSummary:
    return UyumsoftInvoiceSummary(
        invoice_id=invoice_id,
        ettn="ettn-1" if invoice_id else None,
        invoice_number="INV-1",
        invoice_date=datetime(2026, 7, 20, tzinfo=UTC),
        currency="TRY",
        direction="Inbox",
    )
