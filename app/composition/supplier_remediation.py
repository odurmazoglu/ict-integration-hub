from __future__ import annotations

from sqlalchemy.orm import Session

from app.application.use_cases.reclassify_review import ReclassifyWorkbenchReviewUseCase
from app.application.workbench import (
    ResolveWorkbenchSupplierUseCase,
    ValidateSupplierResolutionUseCase,
)
from app.composition.imports import build_deterministic_decision_engine
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.partner_repository import OdooPartnerRepository
from app.erp.odoo.supplier_resolution_partner_reader import OdooSupplierResolutionPartnerReader
from app.erp.write.odoo_supplier_partner_writer import (
    OdooSupplierPartnerRepository,
    OdooSupplierPartnerWritePolicy,
    OdooSupplierPartnerWriter,
)
from app.persistence import (
    SqlAlchemyReviewRepository,
    SqlAlchemyReviewSourceInvoiceEvidenceReader,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyReviewSupplierResolutionRepository,
)


def build_resolve_workbench_supplier_use_case(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> ResolveWorkbenchSupplierUseCase:
    """Compose the explicit supplier remediation orchestration.

    * MATCH_EXISTING re-validates the selected partner read-only.
    * CREATE_PERMANENT_SUPPLIER goes through the controlled, gated
      ``OdooSupplierPartnerWriter`` (default ``SUPPLIER_REMEDIATION_WRITE_ENABLED=false``).
    * Reclassification reuses the same production deterministic ``DecisionEngine``
      as first-time import, so the normal ``PartnerMatchingEngine`` sees the
      selected/created partner in live Odoo state.
    """

    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    read_adapter = OdooReadOnlyAdapter(client=resolved_odoo_client)
    review_repository = SqlAlchemyReviewRepository(session)
    source_invoice_reader = SqlAlchemyReviewSourceInvoiceEvidenceReader(session)

    resolution_validator = ValidateSupplierResolutionUseCase(
        source_invoice_reader=source_invoice_reader,
        partner_reader=OdooSupplierResolutionPartnerReader(
            partner_repository=OdooPartnerRepository(adapter=read_adapter),
        ),
    )

    supplier_partner_writer = OdooSupplierPartnerWriter(
        repository=OdooSupplierPartnerRepository(client=resolved_odoo_client),
        policy=OdooSupplierPartnerWritePolicy.from_settings(settings),
    )

    reclassifier = ReclassifyWorkbenchReviewUseCase(
        decision_engine=build_deterministic_decision_engine(
            session=session,
            settings=settings,
            odoo_client=resolved_odoo_client,
        ),
        source_invoice_reader=source_invoice_reader,
        reclassification_writer=review_repository,
    )

    return ResolveWorkbenchSupplierUseCase(
        review_reader=review_repository,
        source_invoice_reader=source_invoice_reader,
        resolution_validator=resolution_validator,
        resolution_writer=SqlAlchemyReviewSupplierResolutionRepository(session),
        remediation_effect_writer=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        supplier_partner_writer=supplier_partner_writer,
        reclassifier=reclassifier,
    )
