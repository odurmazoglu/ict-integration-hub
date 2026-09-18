from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_db_session, get_request_context
from app.api.error_handling import install_api_exception_handlers
from app.api.routers.workbench import router
from app.api.security import AuthenticationMethod, Permission, RequestContext
from app.connectors.exceptions import ConnectorTimeoutError
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings, get_settings
from app.core.runtime_checks import PRODUCTION_APPROVAL_ACK
from app.db.base import Base
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_one_off_vendor_retirement import WorkbenchReviewOneOffVendorRetirement
from app.persistence import SqlAlchemyVendorBillExecutionEvidenceReader

URL = "/api/workbench/reviews/review-1/one-off-vendor-retirement"


@pytest.fixture
def harness(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            WorkbenchReviewItem(
                review_id="review-1",
                company_id=7,
                invoice_id="ettn",
                invoice_number="INV",
                supplier_tax_number="1234567890",
                supplier_name="Vendor",
                invoice_date=date(2026, 9, 1),
                currency="TRY",
                total_amount=Decimal("100"),
                workflow="vendor_bill",
                status="decision_submitted",
                review_reasons=[],
                warnings=[],
                version=3,
                idempotency_key="review-1",
            )
        )
        session.add(
            WorkbenchReviewOneOffVendorRetirement(
                review_id="review-1",
                company_id=7,
                review_version=2,
                resolved_partner_id=448,
                status="pending_vendor_bill",
            )
        )
        session.commit()
    erp = Mock()
    erp.active = True
    erp.read_error = None
    erp.write_error = None

    async def read(**kwargs):
        assert kwargs["model"] == "res.partner"
        assert kwargs["domain"][0] == ["id", "=", 448]
        if erp.read_error:
            raise erp.read_error
        return [{"id": 448, "name": "Vendor", "vat": "1234567890", "active": erp.active}]

    async def archive(*, partner_id):
        assert partner_id == 448
        erp.active = False
        if erp.write_error:
            raise erp.write_error
        return True

    erp.search_read = AsyncMock(side_effect=read)
    erp.archive_res_partner = AsyncMock(side_effect=archive)
    client_factory = Mock(return_value=erp)
    monkeypatch.setattr(OdooJson2Client, "from_settings", client_factory)
    evidence = Mock(return_value=True)
    monkeypatch.setattr(SqlAlchemyVendorBillExecutionEvidenceReader, "has_successful_vendor_bill", evidence)
    context = RequestContext(
        user_id="operator",
        company_id=7,
        trace_id="trace",
        authentication_method=AuthenticationMethod.JWT,
        permissions=(Permission.WORKBENCH_REVIEW_READ, Permission.WORKBENCH_EXECUTE),
    )
    settings = Settings(
        app_env="production",
        supplier_remediation_write_enabled=True,
        production_operations_enabled=True,
        production_approval_ack=PRODUCTION_APPROVAL_ACK,
    )
    app = FastAPI()
    install_api_exception_handlers(app)
    app.include_router(router)

    def db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_request_context] = lambda: context
    with TestClient(app) as client:
        yield client, engine, erp, app, context, settings, client_factory, evidence
    engine.dispose()


def seed_state(engine, status):
    with Session(engine) as session:
        row = session.scalar(select(WorkbenchReviewOneOffVendorRetirement))
        row.status = status
        session.commit()


def state(engine):
    with Session(engine) as session:
        return session.scalar(select(WorkbenchReviewOneOffVendorRetirement.status))


@pytest.mark.parametrize("status", ["pending_vendor_bill", "archive_attempted", "archived", "needs_reconciliation"])
def test_status_is_persisted_and_read_only(harness, status):
    client, engine, erp, _, _, _, factory, evidence = harness
    seed_state(engine, status)
    response = client.get(URL)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data == {
        "review_id": "review-1",
        "company_id": 7,
        "review_version": 2,
        "resolved_partner_id": 448,
        "status": status,
    }
    assert client.get(URL + "?review_version=2").json()["data"] == data
    assert state(engine) == status
    factory.assert_not_called()
    evidence.assert_not_called()
    erp.archive_res_partner.assert_not_called()


@pytest.mark.parametrize("method", ["get", "post"])
def test_company_isolation_fails_before_erp(harness, method):
    client, engine, _, app, context, _, factory, _ = harness
    app.dependency_overrides[get_request_context] = lambda: replace(context, company_id=8)
    response = client.get(URL) if method == "get" else client.post(URL + "/recover", json={"review_version": 2})
    assert response.status_code == 404
    assert "448" not in response.text
    assert state(engine) == "pending_vendor_bill"
    factory.assert_not_called()


@pytest.mark.parametrize("method", ["get", "post"])
def test_permission_denial_is_read_write_separated(harness, method):
    client, _, _, app, context, _, factory, _ = harness
    other_permission = Permission.WORKBENCH_EXECUTE if method == "get" else Permission.WORKBENCH_REVIEW_READ
    app.dependency_overrides[get_request_context] = lambda: replace(context, permissions=(other_permission,))
    response = client.get(URL) if method == "get" else client.post(URL + "/recover", json={"review_version": 2})
    assert response.status_code == 403
    factory.assert_not_called()


def test_success_and_archived_replay_use_existing_use_case(harness):
    client, engine, erp, _, _, _, _, _ = harness
    first = client.post(URL + "/recover", json={"review_version": 2})
    assert first.status_code == 200
    assert first.json()["data"]["status"] == "archived"
    assert not first.json()["data"]["already_applied"]
    assert state(engine) == "archived"
    second = client.post(URL + "/recover", json={"review_version": 2})
    assert second.status_code == 200
    assert second.json()["data"]["already_applied"]
    erp.archive_res_partner.assert_awaited_once_with(partner_id=448)
    assert erp.search_read.await_count == 2


@pytest.mark.parametrize("status", ["archive_attempted", "needs_reconciliation"])
def test_uncertain_checkpoint_reads_back_before_recovery(harness, status):
    client, engine, erp, _, _, _, _, _ = harness
    seed_state(engine, status)
    erp.active = False
    response = client.post(URL + "/recover", json={"review_version": 2})
    assert response.status_code == 200
    assert state(engine) == "archived"
    erp.search_read.assert_awaited_once()
    erp.archive_res_partner.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"supplier_remediation_write_enabled": False},
        {"production_operations_enabled": False},
        {"production_approval_ack": ""},
    ],
)
def test_gate_closed_prevents_archive_write(harness, changes):
    client, engine, erp, app, _, settings, _, _ = harness
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(update=changes)
    response = client.post(URL + "/recover", json={"review_version": 2})
    assert response.status_code == 403
    assert state(engine) == "pending_vendor_bill"
    erp.archive_res_partner.assert_not_called()


def test_transport_uncertainty_is_persisted_and_recovered_without_duplicate_write(harness):
    client, engine, erp, _, _, _, _, _ = harness
    erp.write_error = ConnectorTimeoutError("Remote write outcome is uncertain")
    response = client.post(URL + "/recover", json={"review_version": 2})
    assert response.status_code == 500
    assert state(engine) == "needs_reconciliation"
    assert client.get(URL).json()["data"]["status"] == "needs_reconciliation"
    resumed = client.post(URL + "/recover", json={"review_version": 2})
    assert resumed.status_code == 200
    assert state(engine) == "archived"
    erp.archive_res_partner.assert_awaited_once()


def test_unavailable_read_back_never_blindly_reissues_archive(harness):
    client, engine, erp, _, _, _, _, _ = harness
    seed_state(engine, "archive_attempted")
    erp.read_error = ConnectorTimeoutError("Read-back unavailable")
    assert client.post(URL + "/recover", json={"review_version": 2}).status_code == 500
    assert state(engine) == "needs_reconciliation"
    assert client.post(URL + "/recover", json={"review_version": 2}).status_code == 500
    erp.archive_res_partner.assert_not_called()


def test_pending_without_durable_bill_evidence_does_not_archive(harness):
    client, engine, erp, _, _, _, _, evidence = harness
    evidence.return_value = False
    response = client.post(URL + "/recover", json={"review_version": 2})
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "awaiting_vendor_bill"
    assert state(engine) == "pending_vendor_bill"
    erp.search_read.assert_not_called()
    erp.archive_res_partner.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"review_version": 0},
        {"review_version": 2, "partner_id": 999},
        {"review_version": 2, "company_id": 8},
        {"review_version": 2, "approved_by": "someone"},
    ],
)
def test_recovery_rejects_identity_and_approval_injection(harness, payload):
    client, _, _, _, _, _, factory, _ = harness
    assert client.post(URL + "/recover", json=payload).status_code == 400
    factory.assert_not_called()


def test_missing_version_and_unknown_query_fail_closed(harness):
    client, _, _, _, _, _, factory, _ = harness
    assert client.get(URL + "?review_version=99").status_code == 404
    assert client.post(URL + "/recover", json={"review_version": 99}).status_code == 404
    assert client.get(URL + "?company_id=8").status_code == 400
    factory.assert_not_called()
