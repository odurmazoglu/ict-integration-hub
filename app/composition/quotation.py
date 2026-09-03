from __future__ import annotations

from sqlalchemy.orm import Session

from app.application.quotation import (
    CaptureAndPersistAcceptedQuotationScenariosUseCase,
    CaptureQuotationScenarioUseCase,
    PersistQuotationScenarioEvidenceUseCase,
)
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.product_repository import OdooProductRepository
from app.erp.odoo.quotation_scenario_source_reader import (
    OdooQuotationScenarioSourceFieldMapping,
    OdooQuotationScenarioSourceReader,
)
from app.persistence import SqlAlchemyQuotationScenarioEvidenceRepository, SqlAlchemyUnitOfWork


def build_capture_and_persist_accepted_quotation_scenarios_use_case(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> CaptureAndPersistAcceptedQuotationScenariosUseCase:
    """Compose the accepted customer-quotation decision evidence orchestration.

    Odoo Proposal Scenario is the read-only authoring source; captured snapshots
    become immutable Hub evidence. No ``sale.order`` write, no Odoo authoring
    write, no execution strategy wiring.
    """

    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    read_adapter = OdooReadOnlyAdapter(client=resolved_odoo_client)
    source_reader = OdooQuotationScenarioSourceReader(
        adapter=read_adapter,
        mapping=OdooQuotationScenarioSourceFieldMapping.from_environment(),
    )
    capture_use_case = CaptureQuotationScenarioUseCase(
        source_reader=source_reader,
        product_variant_reader=OdooProductRepository(adapter=read_adapter),
    )
    persist_use_case = PersistQuotationScenarioEvidenceUseCase(
        repository=SqlAlchemyQuotationScenarioEvidenceRepository(session),
    )
    return CaptureAndPersistAcceptedQuotationScenariosUseCase(
        capture_use_case=capture_use_case,
        persist_use_case=persist_use_case,
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )
