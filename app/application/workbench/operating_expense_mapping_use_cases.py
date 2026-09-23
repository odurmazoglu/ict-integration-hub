"""Operator-facing operating-expense-mapping remediation orchestration (P0-PROD-15P).

Closes the gap identified in P0-PROD-15O: ``OnboardOperatingExpenseMappingUseCase``
already existed but had no supported HTTP path and could never reclassify the
review it was meant to unblock. This module wires it into the same review-scoped,
optimistic-concurrency-checked orchestration shape as
``ResolveWorkbenchSupplierUseCase``/``CreateNewProductUseCase``:

    eligibility check (pending, expected_version, OPERATING_EXPENSE_MAPPING_REQUIRED)
    -> resolved supplier derived ONLY from the review's own accepted
       SupplierRemediationEffect (mirrors CreateNewProductUseCase exactly -- a review
       whose supplier was naturally matched without ever needing remediation is a
       deliberately smaller, out-of-scope case; see the module's own PR description)
    -> selected expense account re-validated read-only (exists, eligible type,
       company-scoped) -- never trusted from the caller as-is
    -> OnboardOperatingExpenseMappingUseCase (REUSED, not reimplemented) persists the
       immutable mapping, idempotently
    -> the existing SUPPLIER_RESOLUTION-shaped MASTER_DATA_CHANGED reclassification
       reruns the real deterministic DecisionEngine, which now sees the durable mapping
    -> report the post-reclassification workflow/reasons; never claim RESOLVED while
       OPERATING_EXPENSE_MAPPING_REQUIRED is still present.

This use case depends only on ports / other use cases -- never on a concrete Odoo client.
"""

from __future__ import annotations

from typing import Protocol

from app.application.exceptions import ApplicationError
from app.application.expense_mapping import (
    OnboardOperatingExpenseMappingCommand,
    OnboardOperatingExpenseMappingUseCase,
    OperatingExpenseMappingOnboardingOutcome,
    OperatingExpenseMappingOnboardingRepository,
)
from app.application.services import UnitOfWork
from app.application.workbench.exceptions import (
    OperatingExpenseMappingAccountInvalidError,
    OperatingExpenseMappingEligibilityError,
    OperatingExpenseMappingSupplierUnresolvedError,
    OperatingExpenseMappingWorkflowError,
    ReviewPersistenceError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
    WorkbenchContractError,
)
from app.application.workbench.operating_expense_mapping_command import (
    OperatingExpenseMappingSubmissionResult,
    OperatingExpenseMappingSubmissionStatus,
    SubmitOperatingExpenseMappingCommand,
)
from app.application.workbench.ports import (
    ExpenseAccountCandidateReader,
    ReviewQueueReader,
    SupplierRemediationEffectWriter,
)
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.reclassification import ReclassifyReviewCommand, ReviewReclassificationTrigger
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode

SAFE_OPERATING_EXPENSE_REMEDIATION_ERROR = "Operating-expense mapping remediation failed."


class OperatingExpenseReclassifier(Protocol):
    """Structural type for ``ReclassifyWorkbenchReviewUseCase``.

    Kept structural, exactly like ``supplier_remediation_use_cases.SupplierReclassifier``,
    so this module never imports ``app.application.use_cases`` (which imports this
    package), avoiding a package-init import cycle.
    """

    async def execute(self, command: ReclassifyReviewCommand): ...


class SubmitOperatingExpenseMappingUseCase:
    """Application boundary for one authenticated operating-expense-mapping decision."""

    def __init__(
        self,
        *,
        review_reader: ReviewQueueReader,
        remediation_effect_reader: SupplierRemediationEffectWriter,
        expense_account_reader: ExpenseAccountCandidateReader,
        mapping_repository: OperatingExpenseMappingOnboardingRepository,
        onboarding_use_case: OnboardOperatingExpenseMappingUseCase,
        reclassifier: OperatingExpenseReclassifier,
        unit_of_work: UnitOfWork,
    ) -> None:
        self._review_reader = review_reader
        self._remediation_effect_reader = remediation_effect_reader
        self._expense_account_reader = expense_account_reader
        self._mapping_repository = mapping_repository
        self._onboarding_use_case = onboarding_use_case
        self._reclassifier = reclassifier
        self._unit_of_work = unit_of_work

    async def execute(self, command: SubmitOperatingExpenseMappingCommand) -> OperatingExpenseMappingSubmissionResult:
        if not isinstance(command, SubmitOperatingExpenseMappingCommand):
            raise WorkbenchContractError("A canonical SubmitOperatingExpenseMappingCommand is required.")

        review = self._review_reader.get_review_item(
            ReviewDetailQuery(review_id=command.review_id, company_id=command.company_id)
        )

        if review.version > command.expected_version:
            resumed = self._resume_if_already_applied(command, review)
            if resumed is not None:
                return resumed
            raise ReviewVersionConflictError("The review version does not match expected_version.")
        if review.version != command.expected_version:
            raise ReviewVersionConflictError("The review version does not match expected_version.")
        self._require_eligible(review)

        vendor_partner_id = self._require_resolved_supplier(command)
        account = self._require_eligible_account(command)

        try:
            onboarding_result = self._onboarding_use_case.execute(
                OnboardOperatingExpenseMappingCommand(
                    company_id=command.company_id,
                    vendor_partner_id=vendor_partner_id,
                    expense_account_id=account.id,
                    expense_category=command.expense_category,
                    enabled=True,
                )
            )
            self._unit_of_work.commit()
            reclass = await self._reclassify(command)
            self._unit_of_work.commit()
        except BaseException:
            self._unit_of_work.rollback()
            raise

        return self._result_from_reclass(
            command,
            vendor_partner_id=vendor_partner_id,
            account_id=account.id,
            onboarding_result=onboarding_result,
            reclass=reclass,
            already_applied=False,
        )

    # ------------------------------------------------------------------ eligibility

    def _require_eligible(self, review) -> None:
        from app.application.workbench.dto import ReviewStatus

        if review.status is not ReviewStatus.PENDING_REVIEW:
            raise ReviewStateConflictError("The review is not pending review.")
        if not _has_operating_expense_mapping_required(review.review_reasons):
            raise OperatingExpenseMappingEligibilityError(
                "The review no longer carries OPERATING_EXPENSE_MAPPING_REQUIRED; there is nothing to remediate."
            )

    def _require_resolved_supplier(self, command: SubmitOperatingExpenseMappingCommand) -> int:
        effect = self._remediation_effect_reader.find_latest_remediation_effect(
            review_id=command.review_id,
            company_id=command.company_id,
        )
        if effect is None:
            raise OperatingExpenseMappingSupplierUnresolvedError(
                "No accepted supplier resolution exists for this review; resolve the supplier first."
            )
        return effect.resolved_partner_id

    def _require_eligible_account(self, command: SubmitOperatingExpenseMappingCommand):
        try:
            account = self._expense_account_reader.find_eligible_by_id(
                company_id=command.company_id,
                account_id=command.expense_account_id,
            )
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001 - translated to a safe workflow error
            raise OperatingExpenseMappingWorkflowError(SAFE_OPERATING_EXPENSE_REMEDIATION_ERROR) from exc
        if account is None:
            raise OperatingExpenseMappingAccountInvalidError(
                "The selected expense account does not exist, is not an eligible operating-expense "
                "account, or is not scoped to this company."
            )
        return account

    # ------------------------------------------------------------------ reclassification

    async def _reclassify(self, command: SubmitOperatingExpenseMappingCommand):
        try:
            return await self._reclassifier.execute(
                ReclassifyReviewCommand(
                    review_id=command.review_id,
                    company_id=command.company_id,
                    expected_version=command.expected_version,
                    trigger=ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
                    note=command.note,
                )
            )
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001 - translated to a safe workflow error
            raise ReviewPersistenceError(SAFE_OPERATING_EXPENSE_REMEDIATION_ERROR) from exc

    def _result_from_reclass(
        self,
        command: SubmitOperatingExpenseMappingCommand,
        *,
        vendor_partner_id: int,
        account_id: int,
        onboarding_result,
        reclass,
        already_applied: bool,
    ) -> OperatingExpenseMappingSubmissionResult:
        still_required = _has_operating_expense_mapping_required(reclass.new_review_reasons)
        status = (
            OperatingExpenseMappingSubmissionStatus.REMEDIATION_INCOMPLETE
            if still_required
            else OperatingExpenseMappingSubmissionStatus.RESOLVED
        )
        return OperatingExpenseMappingSubmissionResult(
            review_id=command.review_id,
            company_id=command.company_id,
            status=status,
            previous_version=reclass.from_version,
            current_version=reclass.to_version,
            current_workflow=reclass.new_workflow,
            current_review_reasons=reclass.new_review_reasons,
            vendor_partner_id=vendor_partner_id,
            expense_account_id=account_id,
            expense_category=onboarding_result.mapping.expense_category,
            mapping_outcome=onboarding_result.outcome,
            reclassified=bool(reclass.changed),
            already_applied=already_applied,
            safe_message=(
                "Operating-expense mapping configured; the review was reclassified."
                if status is OperatingExpenseMappingSubmissionStatus.RESOLVED
                else (
                    "The operating-expense mapping was recorded and the review reclassified, but the review "
                    "still requires an operating-expense mapping. Another blocker may remain."
                )
            ),
        )

    # ------------------------------------------------------------------ resume / idempotency

    def _resume_if_already_applied(
        self,
        command: SubmitOperatingExpenseMappingCommand,
        review,
    ) -> OperatingExpenseMappingSubmissionResult | None:
        """The review already advanced past ``expected_version``.

        A retry of the *exact same* logical request (same account/category, for the same
        already-resolved supplier) is recognized and reported truthfully from the review's
        current, already-committed state -- never a duplicate mapping write, never a
        duplicate reclassification. Anything else (a genuinely stale version, or a
        materially different request) still fails closed as a version conflict.
        """

        effect = self._remediation_effect_reader.find_latest_remediation_effect(
            review_id=command.review_id,
            company_id=command.company_id,
        )
        if effect is None:
            return None
        mapping = self._mapping_repository.find_for_supplier(
            company_id=command.company_id,
            vendor_partner_id=effect.resolved_partner_id,
        )
        if mapping is None:
            return None
        if mapping.expense_account_id != command.expense_account_id or mapping.expense_category != (
            command.expense_category.strip()
        ):
            return None
        still_required = _has_operating_expense_mapping_required(review.review_reasons)
        status = (
            OperatingExpenseMappingSubmissionStatus.REMEDIATION_INCOMPLETE
            if still_required
            else OperatingExpenseMappingSubmissionStatus.RESOLVED
        )
        return OperatingExpenseMappingSubmissionResult(
            review_id=command.review_id,
            company_id=command.company_id,
            status=status,
            previous_version=command.expected_version,
            current_version=review.version,
            current_workflow=review.workflow,
            current_review_reasons=review.review_reasons,
            vendor_partner_id=effect.resolved_partner_id,
            expense_account_id=mapping.expense_account_id,
            expense_category=mapping.expense_category,
            mapping_outcome=OperatingExpenseMappingOnboardingOutcome.ALREADY_CONFIGURED,
            reclassified=True,
            already_applied=True,
            safe_message="This operating-expense mapping remediation was already applied.",
        )


def _has_operating_expense_mapping_required(reasons: tuple[ManualReviewReason, ...]) -> bool:
    return any(reason.code is ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED for reason in reasons)


__all__ = ["OperatingExpenseReclassifier", "SubmitOperatingExpenseMappingUseCase"]
