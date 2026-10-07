from __future__ import annotations

from sqlalchemy.orm import Session

from app.application.expense_mapping import OperatingExpenseMatchingEngine
from app.application.use_cases.reclassify_review import ReclassifyWorkbenchReviewUseCase
from app.application.workbench import (
    ResolveWorkbenchSupplierUseCase,
    ValidateSupplierResolutionUseCase,
)
from app.application.workbench.retirement_recovery import GetOneOffVendorRetirementUseCase
from app.composition.imports import (
    build_deterministic_decision_engine,
    build_runtime_workbench_projection_synchronizer,
)
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.partner_repository import OdooPartnerRepository
from app.erp.odoo.supplier_resolution_partner_reader import OdooSupplierResolutionPartnerReader
from app.erp.write.odoo_supplier_partner_writer import (
    OdooPartnerClassificationFieldConfig,
    OdooSupplierPartnerRepository,
    OdooSupplierPartnerWritePolicy,
    OdooSupplierPartnerWriter,
)
from app.persistence import (
    SqlAlchemyOperatingExpenseMappingRepository,
    SqlAlchemyReviewAccountingResolutionRepository,
    SqlAlchemyReviewOneOffVendorRetirementRepository,
    SqlAlchemyReviewRepository,
    SqlAlchemyReviewSourceInvoiceEvidenceReader,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyReviewSupplierResolutionRepository,
    SqlAlchemyUnitOfWork,
    SqlAlchemyWriteAuthorizationRepository,
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
        classification_config=OdooPartnerClassificationFieldConfig.from_settings(settings),
    )

    reclassifier = ReclassifyWorkbenchReviewUseCase(
        decision_engine=build_deterministic_decision_engine(
            session=session,
            settings=settings,
            odoo_client=resolved_odoo_client,
        ),
        source_invoice_reader=source_invoice_reader,
        reclassification_writer=review_repository,
        # P0-PROD-10D: substitutes this exact review's accepted remediation effect partner
        # into execution evidence when the raw match is not MATCHED. Kept for historical
        # compatibility (pre-redesign archived ONE_OFF_VENDOR effects and MATCH_EXISTING
        # ambiguity) -- new ONE_OFF_VENDOR partners stay active and match on their own.
        supplier_remediation_effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        # P0-PROD-15P: same persistent mapping table the production DecisionEngine's own
        # rule engine queries (a fresh, session-scoped instance) -- lets a MATCH_EXISTING
        # reclassification also resolve OPERATING_EXPENSE_MAPPING_REQUIRED once a real
        # mapping exists, exactly mirroring the P0-PROD-10D-style Stage-1 substitution.
        operating_expense_matcher=OperatingExpenseMatchingEngine(SqlAlchemyOperatingExpenseMappingRepository(session)),
        # P0-PROD-15T: a review-scoped ReviewAccountingResolution (if any) always takes
        # precedence over the supplier-wide mapping above -- see
        # ReclassifyWorkbenchReviewUseCase's own module docstring for the precedence order.
        review_accounting_resolution_reader=SqlAlchemyReviewAccountingResolutionRepository(session),
    )

    # OPS-UI-01A: the canonical full-snapshot synchronizer (None unless the existing
    # ODOO_WORKBENCH_PROJECTION_PUBLISH_ENABLED flag is set) replaces the legacy
    # update-only republisher.
    projection_synchronizer = build_runtime_workbench_projection_synchronizer(
        session=session, settings=settings, odoo_client=resolved_odoo_client
    )

    return ResolveWorkbenchSupplierUseCase(
        review_reader=review_repository,
        source_invoice_reader=source_invoice_reader,
        resolution_validator=resolution_validator,
        resolution_writer=SqlAlchemyReviewSupplierResolutionRepository(session),
        remediation_effect_writer=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        supplier_partner_writer=supplier_partner_writer,
        reclassifier=reclassifier,
        unit_of_work=SqlAlchemyUnitOfWork(session),
        projection_synchronizer=projection_synchronizer,
        # Read-only: reports historical retirement rows; new resolutions never create one.
        retirement_writer=SqlAlchemyReviewOneOffVendorRetirementRepository(session),
        write_authorization_repository=SqlAlchemyWriteAuthorizationRepository(session),
        # The writer itself is the read-only create guard (same Odoo client, same rule).
        create_duplicate_guard=supplier_partner_writer,
    )


def build_get_one_off_vendor_retirement_use_case(*, session: Session) -> GetOneOffVendorRetirementUseCase:
    return GetOneOffVendorRetirementUseCase(
        review_reader=SqlAlchemyReviewRepository(session),
        retirement_reader=SqlAlchemyReviewOneOffVendorRetirementRepository(session),
    )
