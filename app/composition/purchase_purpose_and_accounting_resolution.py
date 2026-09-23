from __future__ import annotations

from sqlalchemy.orm import Session

from app.application.expense_mapping import OperatingExpenseMatchingEngine
from app.application.use_cases.reclassify_review import ReclassifyWorkbenchReviewUseCase
from app.application.workbench.accounting_resolution_use_cases import SubmitReviewAccountingResolutionUseCase
from app.application.workbench.purchase_purpose_use_cases import SubmitPurchasePurposeUseCase
from app.composition.imports import build_deterministic_decision_engine
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.expense_account_candidate_reader import OdooExpenseAccountCandidateReader
from app.persistence import (
    SqlAlchemyOperatingExpenseMappingRepository,
    SqlAlchemyReviewAccountingResolutionRepository,
    SqlAlchemyReviewPurchasePurposeResolutionRepository,
    SqlAlchemyReviewRepository,
    SqlAlchemyReviewSourceInvoiceEvidenceReader,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyUnitOfWork,
)


def build_submit_purchase_purpose_use_case(
    *,
    session: Session,
) -> SubmitPurchasePurposeUseCase:
    """Compose the review-scoped purchase-purpose orchestration (P0-PROD-15T).

    No Odoo client at all: recording a purchase purpose never reclassifies and never
    reads/writes anything ERP-side.
    """

    return SubmitPurchasePurposeUseCase(
        review_reader=SqlAlchemyReviewRepository(session),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        purpose_writer=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


def build_submit_review_accounting_resolution_use_case(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> SubmitReviewAccountingResolutionUseCase:
    """Compose the review-scoped accounting-resolution orchestration (P0-PROD-15T).

    Reuses ``ReclassifyWorkbenchReviewUseCase`` (same class every other Workbench
    remediation composer uses) so the review is reclassified by rerunning the exact
    same production deterministic ``DecisionEngine``, with this exact review's
    accounting resolution wired in as the reclassifier's highest-precedence
    operating-expense override -- see that use case's own module docstring.
    """

    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    read_adapter = OdooReadOnlyAdapter(client=resolved_odoo_client)
    review_repository = SqlAlchemyReviewRepository(session)
    accounting_resolution_repository = SqlAlchemyReviewAccountingResolutionRepository(session)

    reclassifier = ReclassifyWorkbenchReviewUseCase(
        decision_engine=build_deterministic_decision_engine(
            session=session,
            settings=settings,
            odoo_client=resolved_odoo_client,
        ),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        reclassification_writer=review_repository,
        supplier_remediation_effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        operating_expense_matcher=OperatingExpenseMatchingEngine(SqlAlchemyOperatingExpenseMappingRepository(session)),
        review_accounting_resolution_reader=accounting_resolution_repository,
    )

    return SubmitReviewAccountingResolutionUseCase(
        review_reader=review_repository,
        purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        expense_account_reader=OdooExpenseAccountCandidateReader(adapter=read_adapter),
        accounting_resolution_writer=accounting_resolution_repository,
        reclassifier=reclassifier,
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


__all__ = [
    "build_submit_purchase_purpose_use_case",
    "build_submit_review_accounting_resolution_use_case",
]
