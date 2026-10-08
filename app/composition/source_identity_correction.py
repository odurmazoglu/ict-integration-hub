from __future__ import annotations

from sqlalchemy.orm import Session

from app.application.expense_mapping import OperatingExpenseMatchingEngine
from app.application.use_cases.effective_decision import EffectiveDecisionResolver
from app.application.workbench.projection_sync_contracts import ReviewProjectionSynchronizer
from app.application.workbench.source_identity_correction_use_cases import (
    CorrectableSourceInvoiceReader,
    CorrectReviewSourceIdentityUseCase,
    SourceDocumentContentReader,
)
from app.composition.imports import build_deterministic_decision_engine, build_supplier_partner_reader
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.persistence import (
    SqlAlchemyOperatingExpenseMappingRepository,
    SqlAlchemyReviewAccountingResolutionRepository,
    SqlAlchemyReviewRepository,
    SqlAlchemyReviewSourceInvoiceEvidenceReader,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyUnitOfWork,
)
from app.persistence.workbench_review_source_invoice_correction_repository import (
    SqlAlchemyReviewSourceInvoiceCorrectionRepository,
)
from app.services.document_storage import LocalDocumentStorage


def build_correct_review_source_identity_use_case(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
    projection_synchronizer: ReviewProjectionSynchronizer | None = None,
    document_reader: SourceDocumentContentReader | None = None,
) -> CorrectReviewSourceIdentityUseCase:
    """Compose the audited source-identity correction.

    Classification is recalculated through the exact ``EffectiveDecisionResolver``
    inputs every other reclassification uses (production deterministic decision
    engine, supplier-remediation effects, operating-expense matcher, accounting
    resolutions) -- one authoritative computation, only fed the corrected source.
    Odoo is read, never written, by this use case; the only Odoo write is the
    post-commit Workbench projection through ``projection_synchronizer``.
    """

    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    decision_engine = build_deterministic_decision_engine(
        session=session,
        settings=settings,
        odoo_client=resolved_odoo_client,
    )
    review_repository = SqlAlchemyReviewRepository(session)

    def resolver_factory(source_reader: CorrectableSourceInvoiceReader) -> EffectiveDecisionResolver:
        return EffectiveDecisionResolver(
            decision_engine=decision_engine,
            source_invoice_reader=source_reader,
            supplier_remediation_effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
            supplier_partner_reader=build_supplier_partner_reader(settings=settings, odoo_client=resolved_odoo_client),
            operating_expense_matcher=OperatingExpenseMatchingEngine(
                SqlAlchemyOperatingExpenseMappingRepository(session)
            ),
            review_accounting_resolution_reader=SqlAlchemyReviewAccountingResolutionRepository(session),
        )

    correction_repository = SqlAlchemyReviewSourceInvoiceCorrectionRepository(session)
    return CorrectReviewSourceIdentityUseCase(
        review_reader=review_repository,
        source_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        state_reader=correction_repository,
        document_reader=document_reader or LocalDocumentStorage(settings.document_storage_root),
        resolver_factory=resolver_factory,
        writer=correction_repository,
        unit_of_work=SqlAlchemyUnitOfWork(session),
        projection_synchronizer=projection_synchronizer,
    )


__all__ = ["build_correct_review_source_identity_use_case"]
