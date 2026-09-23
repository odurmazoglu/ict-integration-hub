from __future__ import annotations

from sqlalchemy.orm import Session

from app.application.expense_mapping import OnboardOperatingExpenseMappingUseCase, OperatingExpenseMatchingEngine
from app.application.use_cases.reclassify_review import ReclassifyWorkbenchReviewUseCase
from app.application.workbench.expense_account_use_cases import ListExpenseAccountCandidatesUseCase
from app.application.workbench.operating_expense_mapping_use_cases import SubmitOperatingExpenseMappingUseCase
from app.composition.imports import build_deterministic_decision_engine
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.expense_account_candidate_reader import OdooExpenseAccountCandidateReader
from app.persistence import (
    SqlAlchemyOperatingExpenseMappingRepository,
    SqlAlchemyReviewAccountingResolutionRepository,
    SqlAlchemyReviewRepository,
    SqlAlchemyReviewSourceInvoiceEvidenceReader,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyUnitOfWork,
)


def build_list_expense_account_candidates_use_case(
    *,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> ListExpenseAccountCandidatesUseCase:
    """Compose the read-only expense-account lookup (P0-PROD-15P)."""

    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    read_adapter = OdooReadOnlyAdapter(client=resolved_odoo_client)
    return ListExpenseAccountCandidatesUseCase(reader=OdooExpenseAccountCandidateReader(adapter=read_adapter))


def build_submit_operating_expense_mapping_use_case(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> SubmitOperatingExpenseMappingUseCase:
    """Compose the operating-expense-mapping remediation orchestration (P0-PROD-15P).

    Reuses the existing, unchanged ``OnboardOperatingExpenseMappingUseCase`` for the
    mapping write itself, and the existing ``ReclassifyWorkbenchReviewUseCase`` (same
    class ``build_resolve_workbench_supplier_use_case`` composes) so the review is
    reclassified by rerunning the exact same production deterministic ``DecisionEngine``.
    """

    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    read_adapter = OdooReadOnlyAdapter(client=resolved_odoo_client)
    review_repository = SqlAlchemyReviewRepository(session)
    source_invoice_reader = SqlAlchemyReviewSourceInvoiceEvidenceReader(session)
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)

    reclassifier = ReclassifyWorkbenchReviewUseCase(
        decision_engine=build_deterministic_decision_engine(
            session=session,
            settings=settings,
            odoo_client=resolved_odoo_client,
        ),
        source_invoice_reader=source_invoice_reader,
        reclassification_writer=review_repository,
        supplier_remediation_effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        # P0-PROD-15P: the whole point of reclassifying after this endpoint's write --
        # without it, a MATCH_EXISTING-remediated review's raw-ambiguous-forever partner
        # match would keep operating-expense matching stuck at NOT_FOUND even once the
        # mapping this endpoint just onboarded exists.
        operating_expense_matcher=OperatingExpenseMatchingEngine(mapping_repository),
        # P0-PROD-15T: a review-scoped ReviewAccountingResolution (if any) always takes
        # precedence over this endpoint's own supplier-wide mapping -- see
        # ReclassifyWorkbenchReviewUseCase's own module docstring for the precedence order.
        review_accounting_resolution_reader=SqlAlchemyReviewAccountingResolutionRepository(session),
    )

    return SubmitOperatingExpenseMappingUseCase(
        review_reader=review_repository,
        remediation_effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        expense_account_reader=OdooExpenseAccountCandidateReader(adapter=read_adapter),
        mapping_repository=mapping_repository,
        onboarding_use_case=OnboardOperatingExpenseMappingUseCase(mapping_repository),
        reclassifier=reclassifier,
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


__all__ = [
    "build_list_expense_account_candidates_use_case",
    "build_submit_operating_expense_mapping_use_case",
]
