"""Historical ONE_OFF_VENDOR retirement rows: read-only GET stays; recovery is retired.

The archive-after-Vendor-Bill lifecycle was replaced by the expense-vendor
classification redesign. Historical rows are audit evidence: GET keeps reading them
unchanged, and POST .../recover is 410 Gone without touching Hub or Odoo state.
"""

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


def test_get_company_isolation_fails_before_erp(harness):
    client, engine, _, app, context, _, factory, _ = harness
    app.dependency_overrides[get_request_context] = lambda: replace(context, company_id=8)
    response = client.get(URL)
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


@pytest.mark.parametrize("status", ["pending_vendor_bill", "archive_attempted", "archived", "needs_reconciliation"])
@pytest.mark.parametrize(
    "payload",
    [
        {"review_version": 2},
        {"review_version": 2, "authorization_id": "00000000-0000-0000-0000-000000000000"},
        {},
        {"review_version": 99},
        {"review_version": 2, "partner_id": 999},
    ],
)
def test_recover_is_retired_and_never_reads_or_writes_anything(harness, status, payload):
    """The ONE_OFF_VENDOR archive lifecycle is retired: recovery is 410 Gone for every
    historical status and every body, without composing an ERP client, reading Vendor
    Bill evidence or advancing the historical row."""

    client, engine, erp, _, _, _, factory, evidence = harness
    seed_state(engine, status)
    response = client.post(URL + "/recover", json=payload)
    assert response.status_code == 410
    assert response.json()["errors"][0]["code"] == "one_off_vendor_archive_retired"
    assert "448" not in response.text
    assert state(engine) == status
    factory.assert_not_called()
    evidence.assert_not_called()
    erp.archive_res_partner.assert_not_called()


def test_recover_is_retired_even_with_all_write_gates_open_or_closed(harness):
    client, engine, _, app, _, settings, factory, _ = harness
    for changes in ({}, {"supplier_remediation_write_enabled": False}, {"production_operations_enabled": False}):
        app.dependency_overrides[get_settings] = lambda changes=changes: settings.model_copy(update=changes)
        assert client.post(URL + "/recover", json={"review_version": 2}).status_code == 410
    assert state(engine) == "pending_vendor_bill"
    factory.assert_not_called()


def test_missing_version_and_unknown_query_fail_closed(harness):
    client, _, _, _, _, _, factory, _ = harness
    assert client.get(URL + "?review_version=99").status_code == 404
    assert client.get(URL + "?company_id=8").status_code == 400
    factory.assert_not_called()
