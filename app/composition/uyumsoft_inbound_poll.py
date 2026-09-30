from __future__ import annotations

import logging
from functools import partial

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.composition.imports import build_uyumsoft_canonical_invoice_importer, open_read_only_session
from app.connectors.odoo.client import OdooJson2Client
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.core.config import Settings
from app.services.document_storage import DocumentStorage, LocalDocumentStorage
from app.services.uyumsoft_canonical_import import UyumsoftCanonicalInvoiceImporter
from app.services.uyumsoft_inbound_poll import (
    InboundPollConfig,
    InProcessPollLock,
    PollLock,
    PostgresAdvisoryPollLock,
    UyumsoftInboundPollCycle,
    UyumsoftInboundPollPreview,
)

logger = logging.getLogger(__name__)


def build_uyumsoft_inbound_poll_cycle(
    *,
    settings: Settings,
    engine: Engine,
    uyumsoft_client: UyumsoftSoapClient | None = None,
    storage: DocumentStorage | None = None,
    odoo_client: OdooJson2Client | None = None,
) -> UyumsoftInboundPollCycle:
    """Compose one poll cycle from exactly the runtime the manual sync route uses.

    The importer (and, through ``ImportInvoiceUseCase``, the flag-gated runtime
    ``WorkbenchProjectionSynchronizer``) is built per cycle on that cycle's session
    by ``build_uyumsoft_canonical_invoice_importer`` -- the same composer behind
    ``POST /api/v1/sync/uyumsoft/invoices``. No other Odoo write path exists here.
    """

    client = uyumsoft_client or UyumsoftSoapClient.from_settings(settings)
    resolved_storage = storage or LocalDocumentStorage(settings.document_storage_root)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def importer_factory(session: Session) -> UyumsoftCanonicalInvoiceImporter:
        return build_uyumsoft_canonical_invoice_importer(
            session=session,
            settings=settings,
            uyumsoft_client=client,
            storage=resolved_storage,
            odoo_client=odoo_client,
        )

    return UyumsoftInboundPollCycle(
        session_factory=session_factory,
        client=client,
        importer_factory=importer_factory,
        lock=build_poll_lock(engine),
        config=inbound_poll_config(settings),
    )


def build_uyumsoft_inbound_poll_preview(
    *,
    settings: Settings,
    engine: Engine,
    uyumsoft_client: UyumsoftSoapClient | None = None,
) -> UyumsoftInboundPollPreview:
    """Read-only preview with the cycle's own window; Hub reads use a READ ONLY session."""

    return UyumsoftInboundPollPreview(
        read_session_scope=partial(open_read_only_session, engine),
        client=uyumsoft_client or UyumsoftSoapClient.from_settings(settings),
        config=inbound_poll_config(settings),
    )


def build_poll_lock(engine: Engine) -> PollLock:
    if engine.dialect.name == "postgresql":
        return PostgresAdvisoryPollLock(engine)
    logger.warning(
        "uyumsoft_inbound_poll_in_process_lock dialect=%s: cross-process single-flight is PostgreSQL-only",
        engine.dialect.name,
    )
    return InProcessPollLock()


def inbound_poll_config(settings: Settings) -> InboundPollConfig:
    return InboundPollConfig(
        lookback_days=settings.uyumsoft_inbound_poll_lookback_days,
        page_size=settings.uyumsoft_inbound_poll_page_size,
        max_pages=settings.uyumsoft_inbound_poll_max_pages,
    )
