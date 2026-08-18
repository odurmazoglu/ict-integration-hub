from __future__ import annotations

from sqlalchemy.orm import Session

from app.application.decision import DecisionEngine
from app.application.ports import InvoiceImportHistory
from app.application.use_cases import ImportInvoiceUseCase
from app.application.workbench import (
    ReviewItemCreationService,
    WorkbenchClassificationProjectionService,
    WorkbenchProjectionPublisher,
)
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.odoo import (
    OdooWorkbenchJson2ProjectionAdapter,
    OdooWorkbenchProjectionFieldMapping,
    OdooWorkbenchProjectionPublisher,
)
from app.persistence import (
    SqlAlchemyReviewClassificationEvidenceReader,
    SqlAlchemyReviewRepository,
    SqlAlchemyUnitOfWork,
)


def build_import_invoice_use_case(
    *,
    import_history: InvoiceImportHistory,
    decision_engine: DecisionEngine,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> ImportInvoiceUseCase:
    """Compose import runtime with optional best-effort Odoo Workbench projection publishing."""

    publisher = (
        build_odoo_workbench_projection_publisher(session=session, settings=settings, odoo_client=odoo_client)
        if settings.odoo_workbench_projection_publish_enabled
        else None
    )
    return ImportInvoiceUseCase(
        import_history=import_history,
        decision_engine=decision_engine,
        review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(session)),
        workbench_projection_publisher=publisher,
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


def build_odoo_workbench_projection_publisher(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> WorkbenchProjectionPublisher:
    """Build the Odoo publisher with historical classification projection from Hub persistence."""

    adapter = OdooWorkbenchJson2ProjectionAdapter(client=odoo_client or OdooJson2Client.from_settings(settings))
    classification_service = WorkbenchClassificationProjectionService(
        SqlAlchemyReviewClassificationEvidenceReader(session)
    )
    return OdooWorkbenchProjectionPublisher(
        adapter=adapter,
        mapping=OdooWorkbenchProjectionFieldMapping.from_environment(),
        classification_service=classification_service,
    )
