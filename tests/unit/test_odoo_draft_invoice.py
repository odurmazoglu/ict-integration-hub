from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_db_session, get_odoo_client, get_settings
from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.core.config import Settings
from app.db.base import Base
from app.main import app
from app.models.odoo_draft_invoice import OdooDraftInvoice
from app.schemas.odoo_draft_invoice import OdooDraftInvoiceCreateRequest
from app.schemas.odoo_mapping import (
    OdooInvoiceLinePayload,
    OdooJournalCandidate,
    OdooMappingPreview,
    OdooProductCandidate,
    OdooTaxCandidate,
)
from app.services import odoo_draft_invoice
from app.services.document_parser import UblInvoiceParser
from app.services.odoo_draft_invoice import (
    OdooDraftInvoiceConnectorFailure,
    OdooDraftInvoicePersistenceError,
    OdooDraftInvoiceService,
    OdooDraftInvoiceTimeoutFailure,
    OdooDraftInvoiceValidationError,
)
from app.services.odoo_mapping_preview import OdooMappingPreviewService

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "ubl"


class FakeOdooClient:
    def __init__(self, *, result: int = 7001, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def create_account_move(self, payload: dict[str, Any]) -> int:
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def session() -> Generator[Session]:
    factory = _session_factory()
    with factory() as db_session:
        yield db_session


async def test_successful_draft_creation_persists_reference_and_result(session: Session) -> None:
    client = FakeOdooClient(result=9001)
    request = _request(integration_invoice_id=42)

    result = await OdooDraftInvoiceService(session=session, client=client).create_draft(request)
    session.commit()

    assert result.integration_invoice_id == 42
    assert result.ettn == "11111111-2222-3333-4444-555555555555"
    assert result.odoo_model == "account.move"
    assert result.odoo_move_id == 9001
    assert result.creation_status == "created"
    record = session.scalar(select(OdooDraftInvoice))
    assert record is not None
    assert record.creation_status == "created"
    assert record.odoo_move_id == 9001
    assert record.attempt_count == 1


async def test_account_move_draft_request_shape(session: Session) -> None:
    client = FakeOdooClient()

    await OdooDraftInvoiceService(session=session, client=client).create_draft(_request())

    payload = client.calls[0]
    assert payload["move_type"] == "in_invoice"
    assert payload["partner_id"] == 101
    assert payload["currency_id"] == 33
    assert payload["journal_id"] == 55
    assert payload["invoice_date"] == "2026-07-20"
    assert payload["invoice_line_ids"][0][0] == 0
    assert payload["invoice_line_ids"][0][2]["product_id"] == 301
    assert payload["invoice_line_ids"][0][2]["quantity"] == "2.0000"
    assert payload["invoice_line_ids"][0][2]["price_unit"] == "100.0000"
    assert payload["invoice_line_ids"][0][2]["tax_ids"] == [[6, 0, [401]]]
    assert "action_post" not in str(payload)


async def test_repeated_execution_returns_existing_without_second_odoo_call(session: Session) -> None:
    first_client = FakeOdooClient(result=9001)
    service = OdooDraftInvoiceService(session=session, client=first_client)
    await service.create_draft(_request())
    session.commit()
    second_client = FakeOdooClient(result=9002)

    result = await OdooDraftInvoiceService(session=session, client=second_client).create_draft(_request())

    assert result.creation_status == "existing"
    assert result.odoo_move_id == 9001
    assert second_client.calls == []
    assert len(session.scalars(select(OdooDraftInvoice)).all()) == 1


async def test_mapping_preview_not_ready_is_rejected(session: Session) -> None:
    preview = _reviewed_preview()
    preview = preview.model_copy(update={"mapping_status": "needs_review"})

    with pytest.raises(OdooDraftInvoiceValidationError):
        await OdooDraftInvoiceService(session=session, client=FakeOdooClient()).create_draft(_request(preview=preview))


async def test_missing_mandatory_odoo_identifiers_are_rejected(session: Session) -> None:
    preview = _reviewed_preview()
    invoice = preview.invoice.model_copy(
        update={"partner": preview.invoice.partner.model_copy(update={"odoo_id": None})}
    )
    preview = preview.model_copy(update={"invoice": invoice})

    with pytest.raises(OdooDraftInvoiceValidationError) as exc_info:
        await OdooDraftInvoiceService(session=session, client=FakeOdooClient()).create_draft(_request(preview=preview))

    assert "partner.odoo_id" in exc_info.value.safe_message


async def test_connector_authentication_failure_records_safe_failed_attempt(session: Session) -> None:
    client = FakeOdooClient(error=ConnectorError("Odoo authentication failed."))

    with pytest.raises(OdooDraftInvoiceConnectorFailure):
        await OdooDraftInvoiceService(session=session, client=client).create_draft(_request())

    record = session.scalar(select(OdooDraftInvoice))
    assert record is not None
    assert record.creation_status == "failed"
    assert record.safe_error_category == "connector_error"
    assert record.safe_error_message == "Odoo authentication failed."
    assert record.odoo_move_id is None


async def test_timeout_records_safe_failed_attempt(session: Session) -> None:
    client = FakeOdooClient(error=ConnectorTimeoutError("Odoo request timed out."))

    with pytest.raises(OdooDraftInvoiceTimeoutFailure):
        await OdooDraftInvoiceService(session=session, client=client).create_draft(_request())

    record = session.scalar(select(OdooDraftInvoice))
    assert record is not None
    assert record.creation_status == "failed"
    assert record.safe_error_category == "timeout"


async def test_connector_api_failure_records_safe_failed_attempt(session: Session) -> None:
    client = FakeOdooClient(error=ConnectorError("Odoo returned HTTP 503."))

    with pytest.raises(OdooDraftInvoiceConnectorFailure):
        await OdooDraftInvoiceService(session=session, client=client).create_draft(_request())

    record = session.scalar(select(OdooDraftInvoice))
    assert record is not None
    assert record.creation_status == "failed"
    assert record.safe_error_message == "Odoo returned HTTP 503."


async def test_database_claim_failure_rolls_back_before_odoo_call(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
) -> None:
    client = FakeOdooClient()

    def fail_flush() -> None:
        raise SQLAlchemyError("synthetic flush failure")

    monkeypatch.setattr(session, "flush", fail_flush)
    with pytest.raises(OdooDraftInvoicePersistenceError):
        await OdooDraftInvoiceService(session=session, client=client).create_draft(_request())

    assert client.calls == []


async def test_failed_attempt_can_be_retried_without_duplicate(session: Session) -> None:
    with pytest.raises(OdooDraftInvoiceConnectorFailure):
        await OdooDraftInvoiceService(
            session=session,
            client=FakeOdooClient(error=ConnectorError("Odoo unavailable.")),
        ).create_draft(_request())

    result = await OdooDraftInvoiceService(session=session, client=FakeOdooClient(result=9003)).create_draft(_request())

    assert result.creation_status == "created"
    assert result.odoo_move_id == 9003
    record = session.scalar(select(OdooDraftInvoice))
    assert record is not None
    assert record.attempt_count == 2
    assert record.safe_error_category is None


async def test_safe_structured_logging(monkeypatch: pytest.MonkeyPatch, session: Session) -> None:
    log_calls: list[dict[str, Any]] = []

    def capture_log(message: str, *args: Any, **kwargs: Any) -> None:
        log_calls.append({"message": message, **kwargs})

    monkeypatch.setattr(odoo_draft_invoice.logger, "info", capture_log)
    await OdooDraftInvoiceService(session=session, client=FakeOdooClient()).create_draft(_request())

    assert log_calls[0]["message"] == "odoo_draft_invoice_creation_completed"
    assert log_calls[0]["extra"]["ettn"] == "11111111-2222-3333-4444-555555555555"
    assert log_calls[0]["extra"]["operation_status"] == "created"
    assert "Synthetic Supplier" not in str(log_calls)
    assert "Consulting" not in str(log_calls)
    assert "invoice_line_ids" not in str(log_calls)


async def test_endpoint_creates_draft_with_mocked_odoo_client(api_client: AsyncClient) -> None:
    session_factory = _session_factory()
    fake_client = FakeOdooClient(result=8100)

    def db_override() -> Generator[Session]:
        with session_factory() as db_session:
            yield db_session

    app.dependency_overrides[get_settings] = lambda: Settings(app_env="development")
    app.dependency_overrides[get_db_session] = db_override
    app.dependency_overrides[get_odoo_client] = lambda: fake_client
    try:
        response = await api_client.post("/api/v1/odoo/draft-invoices", json=_request().model_dump(mode="json"))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["odoo_move_id"] == 8100
    assert len(fake_client.calls) == 1


async def test_endpoint_blocks_production_environment(api_client: AsyncClient) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(app_env="production")
    app.dependency_overrides[get_odoo_client] = lambda: FakeOdooClient()
    try:
        response = await api_client.post("/api/v1/odoo/draft-invoices", json=_request().model_dump(mode="json"))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_no_uyumsoft_action_post_or_related_record_creation() -> None:
    service_source = (Path(__file__).resolve().parents[2] / "app" / "services" / "odoo_draft_invoice.py").read_text()
    connector_source = (Path(__file__).resolve().parents[2] / "app" / "connectors" / "odoo" / "client.py").read_text()

    combined = service_source + connector_source
    assert "uyumsoft" not in service_source.lower()
    assert "action_post" not in combined
    assert "res.partner/create" not in combined
    assert "product.product/create" not in combined
    assert "account.tax/create" not in combined


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _request(
    *,
    preview: OdooMappingPreview | None = None,
    integration_invoice_id: int | None = None,
) -> OdooDraftInvoiceCreateRequest:
    return OdooDraftInvoiceCreateRequest(
        preview=preview or _reviewed_preview(),
        integration_invoice_id=integration_invoice_id,
        confirm_create_draft=True,
    )


def _reviewed_preview() -> OdooMappingPreview:
    invoice = UblInvoiceParser().parse((FIXTURE_ROOT / "valid_invoice.xml").read_bytes())
    preview = OdooMappingPreviewService().build_preview(invoice)
    reviewed_lines = [
        _reviewed_line(line=line, product_id=301 + index, tax_id=401 if index == 0 else 402)
        for index, line in enumerate(preview.lines)
    ]
    reviewed_invoice = preview.invoice.model_copy(
        update={
            "partner": preview.invoice.partner.model_copy(update={"odoo_id": 101})
            if preview.invoice.partner is not None
            else None,
            "currency_id": 33,
            "journal": OdooJournalCandidate(odoo_id=55, journal_type="purchase", currency="TRY"),
            "invoice_lines": reviewed_lines,
            "taxes": [
                tax.model_copy(update={"odoo_id": 401 + index}) for index, tax in enumerate(preview.invoice.taxes)
            ],
        }
    )
    return preview.model_copy(update={"invoice": reviewed_invoice, "lines": reviewed_lines})


def _reviewed_line(*, line: OdooInvoiceLinePayload, product_id: int, tax_id: int) -> OdooInvoiceLinePayload:
    return line.model_copy(
        update={
            "product": OdooProductCandidate(odoo_id=product_id, lookup_key="name", name=line.product.name)
            if line.product
            else None,
            "taxes": [_reviewed_tax(tax, tax_id=tax_id + index) for index, tax in enumerate(line.taxes)],
        }
    )


def _reviewed_tax(tax: OdooTaxCandidate, *, tax_id: int) -> OdooTaxCandidate:
    return tax.model_copy(update={"odoo_id": tax_id})
