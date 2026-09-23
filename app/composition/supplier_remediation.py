from __future__ import annotations

from sqlalchemy.orm import Session

from app.application.expense_mapping import OperatingExpenseMatchingEngine
from app.application.use_cases.reclassify_review import ReclassifyWorkbenchReviewUseCase
from app.application.workbench import (
    ArchiveOneOffVendorUseCase,
    ResolveWorkbenchSupplierUseCase,
    ValidateSupplierResolutionUseCase,
)
from app.application.workbench.retirement_recovery import (
    GetOneOffVendorRetirementUseCase,
    RecoverOneOffVendorRetirementWorkflow,
)
from app.composition.imports import build_deterministic_decision_engine, build_odoo_workbench_projection_publisher
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.partner_repository import OdooPartnerRepository
from app.erp.odoo.supplier_resolution_partner_reader import OdooSupplierResolutionPartnerReader
from app.erp.write.odoo_one_off_vendor_retirement_writer import OdooOneOffVendorRetirementWriter
from app.erp.write.odoo_supplier_partner_writer import (
    OdooSupplierPartnerRepository,
    OdooSupplierPartnerWritePolicy,
    OdooSupplierPartnerWriter,
)
from app.persistence import (
    SqlAlchemyOperatingExpenseMappingRepository,
    SqlAlchemyReviewOneOffVendorRetirementRepository,
    SqlAlchemyReviewRepository,
    SqlAlchemyReviewSourceInvoiceEvidenceReader,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyReviewSupplierResolutionRepository,
    SqlAlchemyUnitOfWork,
    SqlAlchemyVendorBillExecutionEvidenceReader,
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
    )

    reclassifier = ReclassifyWorkbenchReviewUseCase(
        decision_engine=build_deterministic_decision_engine(
            session=session,
            settings=settings,
            odoo_client=resolved_odoo_client,
        ),
        source_invoice_reader=source_invoice_reader,
        reclassification_writer=review_repository,
        # P0-PROD-10D: lets reclassification reach a submittable decision for an
        # archived Hub-owned ONE_OFF_VENDOR reuse -- see ReclassifyWorkbenchReviewUseCase's
        # own docstring for the exact, narrowly-scoped substitution this enables.
        supplier_remediation_effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        # P0-PROD-15P: same persistent mapping table the production DecisionEngine's own
        # rule engine queries (a fresh, session-scoped instance) -- lets a MATCH_EXISTING
        # reclassification also resolve OPERATING_EXPENSE_MAPPING_REQUIRED once a real
        # mapping exists, exactly mirroring the P0-PROD-10D-style Stage-1 substitution.
        operating_expense_matcher=OperatingExpenseMatchingEngine(SqlAlchemyOperatingExpenseMappingRepository(session)),
    )

    # Reuse the existing best-effort Workbench publisher, gated by the existing flag.
    # It updates the row created at import time; there is no new republish flag.
    workbench_republisher = (
        build_odoo_workbench_projection_publisher(
            session=session,
            settings=settings,
            odoo_client=resolved_odoo_client,
        )
        if settings.odoo_workbench_projection_publish_enabled
        else None
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
        workbench_republisher=workbench_republisher,
        retirement_writer=SqlAlchemyReviewOneOffVendorRetirementRepository(session),
        write_authorization_repository=SqlAlchemyWriteAuthorizationRepository(session),
    )


def build_archive_one_off_vendor_use_case(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
    approved_by: str | None = None,
    authorization_id: str | None = None,
) -> ArchiveOneOffVendorUseCase:
    """Compose the ONE_OFF_VENDOR archive-last orchestration (P0-PROD-08H).

    Reuses the exact same gated, controlled ``OdooSupplierPartnerRepository``/
    ``OdooSupplierPartnerWritePolicy`` as supplier-partner creation -- archiving a
    Hub-owned one-off partner is protected by the same
    ``SUPPLIER_REMEDIATION_WRITE_ENABLED`` authorization, not a new or broader one.
    Used by the post-execution trigger and explicit operator recovery workflow.

    ``authorization_id`` (P0-PROD-09F) is only ever set by the recovery workflow --
    the automatic post-execution trigger never supplies it.
    """

    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    retirement_port = OdooOneOffVendorRetirementWriter(
        repository=OdooSupplierPartnerRepository(client=resolved_odoo_client),
        client=resolved_odoo_client,
        policy=OdooSupplierPartnerWritePolicy.from_settings(settings),
    )
    return ArchiveOneOffVendorUseCase(
        retirement_writer=SqlAlchemyReviewOneOffVendorRetirementRepository(session),
        vendor_bill_evidence_reader=SqlAlchemyVendorBillExecutionEvidenceReader(session),
        retirement_port=retirement_port,
        unit_of_work=SqlAlchemyUnitOfWork(session),
        approved_by=approved_by,
        write_authorization_repository=SqlAlchemyWriteAuthorizationRepository(session),
        authorization_id=authorization_id,
    )


def build_get_one_off_vendor_retirement_use_case(*, session: Session) -> GetOneOffVendorRetirementUseCase:
    return GetOneOffVendorRetirementUseCase(
        review_reader=SqlAlchemyReviewRepository(session),
        retirement_reader=SqlAlchemyReviewOneOffVendorRetirementRepository(session),
    )


def build_recover_one_off_vendor_retirement_workflow(
    *, session: Session, settings: Settings, odoo_client: OdooJson2Client | None = None
) -> RecoverOneOffVendorRetirementWorkflow:
    return RecoverOneOffVendorRetirementWorkflow(
        status_reader=build_get_one_off_vendor_retirement_use_case(session=session),
        archive_use_case_factory=lambda *, approved_by, authorization_id=None: build_archive_one_off_vendor_use_case(
            session=session,
            settings=settings,
            odoo_client=odoo_client,
            approved_by=approved_by,
            authorization_id=authorization_id,
        ),
    )
