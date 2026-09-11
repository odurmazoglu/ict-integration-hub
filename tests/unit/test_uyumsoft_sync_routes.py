from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_db_session, get_uyumsoft_canonical_importer, get_uyumsoft_client
from app.connectors.exceptions import ConnectorError
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.main import app
from app.models.import_receipt import ImportReceipt
from app.models.uyumsoft_invoice import UyumsoftInvoiceMetadata
from app.models.uyumsoft_sync_run import UyumsoftSyncRun
from app.schemas.uyumsoft_invoices import (
    UyumsoftInvoiceListRequest,
    UyumsoftInvoiceListResponse,
    UyumsoftInvoiceSummary,
)
from app.services.uyumsoft_canonical_import import (
    IMPORT_STATUS_ACCEPTED,
    IMPORT_STATUS_REVIEW_CREATED,
    UyumsoftCanonicalImportBatchResult,
    UyumsoftCanonicalImportOutcome,
)


class FakeSyncUyumsoftClient(UyumsoftSoapClient):
    def __init__(self) -> None:
        pass

    def list_inbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        return _response("Inbox", request, "inbox-ettn")

    def list_outbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        return _response("Outbox", request, "outbox-ettn")


class FailingOutboxSyncUyumsoftClient(FakeSyncUyumsoftClient):
    def list_outbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        raise ConnectorError("Outbox transport failed")


class RefusingSyncUyumsoftClient(UyumsoftSoapClient):
    """Fails the test if the Uyumsoft connector is ever reached. Used to prove the sync gate
    denies the request before any connector call, regardless of UYUMSOFT_ENVIRONMENT."""

    def __init__(self) -> None:
        pass

    def list_inbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        raise AssertionError("Uyumsoft connector must not be called when the sync gate is disabled.")

    def list_outbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        raise AssertionError("Uyumsoft connector must not be called when the sync gate is disabled.")


class RefusingCanonicalImporter:
    """Fails the test if the canonical importer is ever reached when the sync gate is disabled."""

    def import_invoices(
        self,
        invoices: list[UyumsoftInvoiceSummary],
        *,
        persisted_records: dict[str, object],
    ) -> UyumsoftCanonicalImportBatchResult:
        raise AssertionError("Canonical importer must not be called when the sync gate is disabled.")


class TwoInboxInvoicesPerPageClient(UyumsoftSoapClient):
    """Real production shape from P0-PROD-06B: two distinct real invoices returned by a
    single provider page. Used to prove the invoice_ettn allowlist selects exactly one."""

    def __init__(self) -> None:
        pass

    def list_inbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        return UyumsoftInvoiceListResponse(
            direction="Inbox",
            page=request.page,
            page_size=request.page_size,
            total_count=2,
            invoices=[
                UyumsoftInvoiceSummary(
                    invoice_id="HD12026000964602",
                    ettn="HD12026000964602",
                    invoice_number="F1ADCCAD-FB70-AAF1-8105-005056BB160E",
                    invoice_date=datetime(2026, 9, 11, 10, 58, 39, tzinfo=UTC),
                    sender=None,
                    receiver="D-MARKET ELEKTRONIK HIZMETLER VE TICARET ANONIM SIRKETI",
                    tax_number="2650179910",
                    currency="TRY",
                    total_amount=Decimal("2599.20"),
                    direction="Inbox",
                    status="Approved",
                ),
                UyumsoftInvoiceSummary(
                    invoice_id="HD12026000964604",
                    ettn="HD12026000964604",
                    invoice_number="F1ADCCAD-FB70-AAF1-8105-005056BB160F",
                    invoice_date=datetime(2026, 9, 11, 10, 58, 41, tzinfo=UTC),
                    sender=None,
                    receiver="D-MARKET ELEKTRONIK HIZMETLER VE TICARET ANONIM SIRKETI",
                    tax_number="2650179910",
                    currency="TRY",
                    total_amount=Decimal("676.21"),
                    direction="Inbox",
                    status="Approved",
                ),
            ],
        )

    def list_outbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        return UyumsoftInvoiceListResponse(
            direction="Outbox", page=request.page, page_size=request.page_size, invoices=[]
        )


class NoopCanonicalImporter:
    def import_invoices(
        self,
        invoices: list[UyumsoftInvoiceSummary],
        *,
        persisted_records: dict[str, object],
    ) -> UyumsoftCanonicalImportBatchResult:
        return UyumsoftCanonicalImportBatchResult()


class RecordingCanonicalImporter:
    def __init__(self) -> None:
        self.calls: list[list[UyumsoftInvoiceSummary]] = []

    def import_invoices(
        self,
        invoices: list[UyumsoftInvoiceSummary],
        *,
        persisted_records: dict[str, object],
    ) -> UyumsoftCanonicalImportBatchResult:
        self.calls.append(invoices)
        return UyumsoftCanonicalImportBatchResult(
            outcomes=tuple(
                UyumsoftCanonicalImportOutcome(
                    direction=invoice.direction,
                    invoice_identity=invoice.ettn or "missing",
                    status=IMPORT_STATUS_REVIEW_CREATED,
                    company_id=7,
                    import_status="review_required",
                    imported_invoice_id=invoice.ettn,
                    review_id="review-1",
                )
                for invoice in invoices
            )
        )


class ReceiptWritingCanonicalImporter:
    def __init__(self, session_holder: list[Session]) -> None:
        self._session_holder = session_holder

    def import_invoices(
        self,
        invoices: list[UyumsoftInvoiceSummary],
        *,
        persisted_records: dict[str, object],
    ) -> UyumsoftCanonicalImportBatchResult:
        invoice = invoices[0]
        receipt = ImportReceipt(
            company_id=7,
            idempotency_key=f"uyumsoft:company:7:{invoice.direction.lower()}:ettn:{invoice.ettn}",
            invoice_id=invoice.ettn or "missing",
            status="dry_run",
            vendor_bill_id=None,
            review_id=None,
        )
        self._session_holder[0].add(receipt)
        self._session_holder[0].flush()
        return UyumsoftCanonicalImportBatchResult(
            outcomes=(
                UyumsoftCanonicalImportOutcome(
                    direction=invoice.direction,
                    invoice_identity=invoice.ettn or "missing",
                    status=IMPORT_STATUS_ACCEPTED,
                    company_id=7,
                    import_status="dry_run",
                    imported_invoice_id=invoice.ettn,
                ),
            )
        )


async def test_sync_endpoint_requires_read_only_confirmation(api_client: AsyncClient) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(uyumsoft_sync_execute_enabled=True)
    try:
        response = await api_client.post(
            "/api/v1/sync/uyumsoft/invoices",
            params={
                "from": "2026-07-16T00:00:00+00:00",
                "to": "2026-07-17T00:00:00+00:00",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert "confirm_read_only" in response.json()["detail"]


async def test_sync_endpoint_denies_when_gate_disabled_with_test_environment(api_client: AsyncClient) -> None:
    """Gate false + UYUMSOFT_ENVIRONMENT=test -> denied, connector/importer never reached."""
    app.dependency_overrides[get_settings] = lambda: Settings(
        uyumsoft_sync_execute_enabled=False, uyumsoft_environment="test"
    )
    app.dependency_overrides[get_uyumsoft_client] = lambda: RefusingSyncUyumsoftClient()
    app.dependency_overrides[get_uyumsoft_canonical_importer] = lambda: RefusingCanonicalImporter()
    try:
        response = await api_client.post(
            "/api/v1/sync/uyumsoft/invoices",
            params={
                "from": "2026-07-16T00:00:00+00:00",
                "to": "2026-07-17T00:00:00+00:00",
                "confirm_read_only": "true",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert "UYUMSOFT_SYNC_EXECUTE_ENABLED" in response.json()["detail"]


async def test_sync_endpoint_denies_when_gate_disabled_with_production_environment(api_client: AsyncClient) -> None:
    """Gate false + UYUMSOFT_ENVIRONMENT=production -> denied, connector/importer never reached.

    This is the production configuration this gate exists to make reachable once explicitly
    enabled; with the gate left at its safe default, it must stay denied exactly like the test
    environment case, and the environment setting itself must be left untouched (no fallback to
    "test", no connector/environment mutation as a side effect of the denial).
    """
    settings = Settings(uyumsoft_sync_execute_enabled=False, uyumsoft_environment="production")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_uyumsoft_client] = lambda: RefusingSyncUyumsoftClient()
    app.dependency_overrides[get_uyumsoft_canonical_importer] = lambda: RefusingCanonicalImporter()
    try:
        response = await api_client.post(
            "/api/v1/sync/uyumsoft/invoices",
            params={
                "from": "2026-07-16T00:00:00+00:00",
                "to": "2026-07-17T00:00:00+00:00",
                "confirm_read_only": "true",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert "UYUMSOFT_SYNC_EXECUTE_ENABLED" in response.json()["detail"]
    assert settings.uyumsoft_environment == "production"


async def test_sync_endpoint_denies_with_invoice_ettn_supplied_when_gate_disabled(api_client: AsyncClient) -> None:
    """Gate false + invoice_ettn supplied -> still denied before any connector/importer call.

    The allowlist is a selection detail of an already-authorized sync; it must never bypass
    or interact with the execute gate itself."""
    app.dependency_overrides[get_settings] = lambda: Settings(uyumsoft_sync_execute_enabled=False)
    app.dependency_overrides[get_uyumsoft_client] = lambda: RefusingSyncUyumsoftClient()
    app.dependency_overrides[get_uyumsoft_canonical_importer] = lambda: RefusingCanonicalImporter()
    try:
        response = await api_client.post(
            "/api/v1/sync/uyumsoft/invoices",
            params={
                "from": "2026-07-16T00:00:00+00:00",
                "to": "2026-07-17T00:00:00+00:00",
                "confirm_read_only": "true",
                "invoice_ettn": "HD12026000964604",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert "UYUMSOFT_SYNC_EXECUTE_ENABLED" in response.json()["detail"]


async def test_sync_endpoint_reaches_workflow_when_gate_enabled_with_production_environment(
    api_client: AsyncClient,
) -> None:
    """Gate true + UYUMSOFT_ENVIRONMENT=production -> the existing workflow is reachable.

    No separate production-only code path is introduced: this exercises the identical
    FakeSyncUyumsoftClient/NoopCanonicalImporter wiring used for the test-environment case.
    """
    session_factory = _session_factory()

    def db_override() -> Generator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: Settings(
        uyumsoft_sync_execute_enabled=True, uyumsoft_environment="production"
    )
    app.dependency_overrides[get_db_session] = db_override
    app.dependency_overrides[get_uyumsoft_client] = lambda: FakeSyncUyumsoftClient()
    app.dependency_overrides[get_uyumsoft_canonical_importer] = lambda: NoopCanonicalImporter()
    try:
        response = await api_client.post(
            "/api/v1/sync/uyumsoft/invoices",
            params={
                "from": "2026-07-16T00:00:00+00:00",
                "to": "2026-07-17T00:00:00+00:00",
                "confirm_read_only": "true",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200


async def test_sync_endpoint_does_not_require_bearer_authentication(api_client: AsyncClient) -> None:
    """The new gate changes only availability, not authentication: an unauthenticated request
    (no Authorization header, exactly as every other test in this file sends it) still reaches
    the same 200 outcome as before once the gate is explicitly enabled -- no auth dependency was
    added or removed by this change."""
    session_factory = _session_factory()

    def db_override() -> Generator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: Settings(uyumsoft_sync_execute_enabled=True)
    app.dependency_overrides[get_db_session] = db_override
    app.dependency_overrides[get_uyumsoft_client] = lambda: FakeSyncUyumsoftClient()
    app.dependency_overrides[get_uyumsoft_canonical_importer] = lambda: NoopCanonicalImporter()
    try:
        response = await api_client.post(
            "/api/v1/sync/uyumsoft/invoices",
            params={
                "from": "2026-07-16T00:00:00+00:00",
                "to": "2026-07-17T00:00:00+00:00",
                "confirm_read_only": "true",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert "authorization" not in {key.lower() for key in response.request.headers.keys()}
    assert response.status_code == 200


async def test_sync_endpoint_persists_read_only_summary(api_client: AsyncClient) -> None:
    session_factory = _session_factory()

    def db_override() -> Generator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: Settings(uyumsoft_sync_execute_enabled=True)
    app.dependency_overrides[get_db_session] = db_override
    app.dependency_overrides[get_uyumsoft_client] = lambda: FakeSyncUyumsoftClient()
    app.dependency_overrides[get_uyumsoft_canonical_importer] = lambda: NoopCanonicalImporter()
    try:
        response = await api_client.post(
            "/api/v1/sync/uyumsoft/invoices",
            params={
                "from": "2026-07-16T00:00:00+00:00",
                "to": "2026-07-17T00:00:00+00:00",
                "direction": "Both",
                "page_size": "10",
                "max_pages": "1",
                "confirm_read_only": "true",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["created"] == 2
    assert body["updated"] == 0
    assert body["skipped"] == 0
    assert body["status"] == "completed"
    assert body["run_id"] is not None
    assert body["cursor_state"]["Inbox"]["current_page"] == 1
    assert body["cursor_state"]["Outbox"]["current_page"] == 1
    with session_factory() as session:
        records = session.scalars(select(UyumsoftInvoiceMetadata)).all()
        sync_run = session.scalar(select(UyumsoftSyncRun))
    assert {record.direction for record in records} == {"Inbox", "Outbox"}
    assert sync_run is not None
    assert sync_run.status == "completed"


async def test_sync_endpoint_records_failed_run_on_connector_error(api_client: AsyncClient) -> None:
    session_factory = _session_factory()

    def db_override() -> Generator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: Settings(uyumsoft_sync_execute_enabled=True)
    app.dependency_overrides[get_db_session] = db_override
    app.dependency_overrides[get_uyumsoft_client] = lambda: FailingOutboxSyncUyumsoftClient()
    app.dependency_overrides[get_uyumsoft_canonical_importer] = lambda: NoopCanonicalImporter()
    try:
        response = await api_client.post(
            "/api/v1/sync/uyumsoft/invoices",
            params={
                "from": "2026-07-16T00:00:00+00:00",
                "to": "2026-07-17T00:00:00+00:00",
                "direction": "Both",
                "page_size": "10",
                "max_pages": "1",
                "confirm_read_only": "true",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    with session_factory() as session:
        sync_run = session.scalar(select(UyumsoftSyncRun))
        records = session.scalars(select(UyumsoftInvoiceMetadata)).all()
    assert sync_run is not None
    assert sync_run.status == "failed"
    assert sync_run.cursor_state["Inbox"]["status"] == "completed"
    assert sync_run.cursor_state["Outbox"]["status"] == "failed"
    assert {record.direction for record in records} == {"Inbox"}


async def test_sync_endpoint_reaches_canonical_importer(api_client: AsyncClient) -> None:
    session_factory = _session_factory()
    importer = RecordingCanonicalImporter()

    def db_override() -> Generator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: Settings(uyumsoft_sync_execute_enabled=True)
    app.dependency_overrides[get_db_session] = db_override
    app.dependency_overrides[get_uyumsoft_client] = lambda: FakeSyncUyumsoftClient()
    app.dependency_overrides[get_uyumsoft_canonical_importer] = lambda: importer
    try:
        response = await api_client.post(
            "/api/v1/sync/uyumsoft/invoices",
            params={
                "from": "2026-07-16T00:00:00+00:00",
                "to": "2026-07-17T00:00:00+00:00",
                "direction": "Inbox",
                "page_size": "10",
                "max_pages": "1",
                "confirm_read_only": "true",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert len(importer.calls) == 1
    assert importer.calls[0][0].direction == "Inbox"
    body = response.json()
    assert body["review_count"] == 1
    assert body["directions"][0]["import_outcomes"][0]["status"] == IMPORT_STATUS_REVIEW_CREATED


async def test_sync_endpoint_invoice_ettn_filter_selects_one_of_two_end_to_end(api_client: AsyncClient) -> None:
    """End-to-end plumbing proof for the P0-PROD-06B blocker: two real invoices on one
    provider page, only the exact requested ettn reaches persistence and the importer."""
    session_factory = _session_factory()
    importer = RecordingCanonicalImporter()

    def db_override() -> Generator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: Settings(uyumsoft_sync_execute_enabled=True)
    app.dependency_overrides[get_db_session] = db_override
    app.dependency_overrides[get_uyumsoft_client] = lambda: TwoInboxInvoicesPerPageClient()
    app.dependency_overrides[get_uyumsoft_canonical_importer] = lambda: importer
    try:
        response = await api_client.post(
            "/api/v1/sync/uyumsoft/invoices",
            params={
                "from": "2026-09-01T00:00:00+00:00",
                "to": "2026-09-11T23:59:59+00:00",
                "direction": "Inbox",
                "confirm_read_only": "true",
                "invoice_ettn": "HD12026000964604",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["directions"][0]["invoices_seen"] == 2
    assert body["selected_invoices"] == 1
    assert body["requested_invoice_ettn"] == ["HD12026000964604"]
    assert body["matched_invoice_ettn"] == ["HD12026000964604"]
    with session_factory() as session:
        records = session.scalars(select(UyumsoftInvoiceMetadata)).all()
    assert [record.ettn for record in records] == ["HD12026000964604"]
    assert len(importer.calls) == 1
    assert [invoice.ettn for invoice in importer.calls[0]] == ["HD12026000964604"]


async def test_sync_endpoint_commits_non_review_receipt_with_request_transaction(api_client: AsyncClient) -> None:
    session_factory = _session_factory()
    session_holder: list[Session] = []

    def db_override() -> Generator[Session]:
        with session_factory() as session:
            session_holder[:] = [session]
            yield session

    app.dependency_overrides[get_settings] = lambda: Settings(uyumsoft_sync_execute_enabled=True)
    app.dependency_overrides[get_db_session] = db_override
    app.dependency_overrides[get_uyumsoft_client] = lambda: FakeSyncUyumsoftClient()
    app.dependency_overrides[get_uyumsoft_canonical_importer] = lambda: ReceiptWritingCanonicalImporter(session_holder)
    try:
        response = await api_client.post(
            "/api/v1/sync/uyumsoft/invoices",
            params={
                "from": "2026-07-16T00:00:00+00:00",
                "to": "2026-07-17T00:00:00+00:00",
                "direction": "Inbox",
                "page_size": "10",
                "max_pages": "1",
                "confirm_read_only": "true",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["directions"][0]["import_outcomes"][0]["status"] == IMPORT_STATUS_ACCEPTED
    with session_factory() as session:
        receipts = session.scalars(select(ImportReceipt)).all()
    assert len(receipts) == 1
    assert receipts[0].idempotency_key == "uyumsoft:company:7:inbox:ettn:inbox-ettn"


async def test_sync_endpoint_does_not_return_accepted_when_receipt_commit_fails(
    api_client: AsyncClient,
) -> None:
    session_factory = _session_factory(session_cls=FailingCommitSession)
    session_holder: list[Session] = []

    def db_override() -> Generator[Session]:
        with session_factory() as session:
            session_holder[:] = [session]
            yield session

    app.dependency_overrides[get_settings] = lambda: Settings(uyumsoft_sync_execute_enabled=True)
    app.dependency_overrides[get_db_session] = db_override
    app.dependency_overrides[get_uyumsoft_client] = lambda: FakeSyncUyumsoftClient()
    app.dependency_overrides[get_uyumsoft_canonical_importer] = lambda: ReceiptWritingCanonicalImporter(session_holder)
    try:
        try:
            await api_client.post(
                "/api/v1/sync/uyumsoft/invoices",
                params={
                    "from": "2026-07-16T00:00:00+00:00",
                    "to": "2026-07-17T00:00:00+00:00",
                    "direction": "Inbox",
                    "page_size": "10",
                    "max_pages": "1",
                    "confirm_read_only": "true",
                },
            )
        except RuntimeError as exc:
            assert str(exc) == "commit failed"
        else:
            raise AssertionError("commit failure must propagate instead of returning an accepted response")
    finally:
        app.dependency_overrides.clear()

    with session_factory() as session:
        receipts = session.scalars(select(ImportReceipt)).all()
    assert receipts == []


def _session_factory(*, session_cls: type[Session] = Session) -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=session_cls)


class FailingCommitSession(Session):
    def commit(self) -> None:
        raise RuntimeError("commit failed")


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
            )
        ],
    )
