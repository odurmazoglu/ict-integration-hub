"""Operator-facing review-scoped accounting-resolution orchestration (P0-PROD-15T).

Ties together the pieces P0-PROD-15T adds, mirroring
``SubmitOperatingExpenseMappingUseCase``'s shape closely:

    eligibility check (pending, expected_version, operating-expense-shaped reason)
    -> accepted PurchasePurposeResolution must already exist for this exact
       (review_id, company_id, review_version) -- purpose before treatment
    -> that purpose must currently support EXPENSE_ACCOUNT (RESALE/CUSTOMER_PROJECT
       fail closed with a precise "not implemented yet" error, never silently
       reinterpreted as a plain expense)
    -> selected expense account re-validated read-only (exists, eligible type,
       company-scoped) -- never trusted from a prior GET
    -> persist the immutable, review-scoped ReviewAccountingResolution (never the
       supplier-wide operating_expense_mappings table)
    -> trigger the existing MASTER_DATA_CHANGED-shaped reclassification, which now
       (P0-PROD-15T) consults this exact resolution ahead of any supplier-wide state
    -> report the post-reclassification workflow/reasons; never claim RESOLVED while
       an operating-expense-shaped reason is still present.
"""

from __future__ import annotations

from typing import Protocol

from app.application.exceptions import ApplicationError
from app.application.services import UnitOfWork
from app.application.workbench.accounting_resolution import (
    AccountingResolutionStatus,
    ReviewAccountingResolution,
    ReviewAccountingResolutionSubmissionResult,
    SubmitReviewAccountingResolutionCommand,
)
from app.application.workbench.exceptions import (
    AccountingResolutionConflictError,
    AccountingResolutionEligibilityError,
    AccountingResolutionPurposeRequiredError,
    AccountingResolutionPurposeUnsupportedError,
    OperatingExpenseMappingAccountInvalidError,
    OperatingExpenseMappingWorkflowError,
    ReviewPersistenceError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
    WorkbenchContractError,
)
from app.application.workbench.expense_account_lookup import ExpenseAccountCandidate
from app.application.workbench.ports import (
    ExpenseAccountCandidateReader,
    PurchasePurposeResolutionWriter,
    ReviewAccountingResolutionWriter,
    ReviewQueueReader,
)
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.reclassification import ReclassifyReviewCommand, ReviewReclassificationTrigger
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode

SAFE_ACCOUNTING_RESOLUTION_ERROR = "Review-scoped accounting resolution failed."

#: Purchase purposes with an implemented EXPENSE_ACCOUNT treatment today. RESALE and
#: CUSTOMER_PROJECT are deliberately absent -- see module docstring; extending this
#: set is a real feature change, not a runtime decision.
_PURPOSES_SUPPORTING_EXPENSE_ACCOUNT = frozenset(
    {PurchasePurpose.INTERNAL_USE, PurchasePurpose.OTHER_OPERATING_EXPENSE}
)


class AccountingResolutionReclassifier(Protocol):
    """Structural type for ``ReclassifyWorkbenchReviewUseCase``.

    Kept structural, exactly like the analogous Protocols in
    ``supplier_remediation_use_cases.py``/``operating_expense_mapping_use_cases.py``,
    so this module never imports ``app.application.use_cases`` (which imports this
    package), avoiding a package-init import cycle.
    """

    async def execute(self, command: ReclassifyReviewCommand): ...


class SubmitReviewAccountingResolutionUseCase:
    """Application boundary for one authenticated review-scoped accounting decision."""

    def __init__(
        self,
        *,
        review_reader: ReviewQueueReader,
        purpose_reader: PurchasePurposeResolutionWriter,
        expense_account_reader: ExpenseAccountCandidateReader,
        accounting_resolution_writer: ReviewAccountingResolutionWriter,
        reclassifier: AccountingResolutionReclassifier,
        unit_of_work: UnitOfWork,
    ) -> None:
        self._review_reader = review_reader
        self._purpose_reader = purpose_reader
        self._expense_account_reader = expense_account_reader
        self._accounting_resolution_writer = accounting_resolution_writer
        self._reclassifier = reclassifier
        self._unit_of_work = unit_of_work

    async def execute(
        self, command: SubmitReviewAccountingResolutionCommand
    ) -> ReviewAccountingResolutionSubmissionResult:
        if not isinstance(command, SubmitReviewAccountingResolutionCommand):
            raise WorkbenchContractError("A canonical SubmitReviewAccountingResolutionCommand is required.")

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

        purpose = self._purpose_reader.find_purchase_purpose_resolution(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=command.expected_version,
        )
        if purpose is None:
            raise AccountingResolutionPurposeRequiredError(
                "No purchase-purpose resolution exists for this review version; record the purpose first."
            )
        if purpose.purchase_purpose not in _PURPOSES_SUPPORTING_EXPENSE_ACCOUNT:
            raise AccountingResolutionPurposeUnsupportedError(
                f"Accounting treatment for purchase_purpose={purpose.purchase_purpose.value} is not implemented "
                "yet; the review remains manual_review until a supported treatment exists."
            )

        account = self._require_eligible_account(command)

        existing = self._accounting_resolution_writer.find_accounting_resolution(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=command.expected_version,
        )
        if existing is not None:
            if not _matches(existing, command):
                raise AccountingResolutionConflictError(
                    "A different accounting resolution already exists for this review version."
                )
            # The resolution row is already durable; a prior attempt's reclassify call
            # may not have completed (e.g. crash between the two commits). Resuming
            # here re-runs reclassification against the SAME persisted row -- never a
            # second resolution row.
            try:
                reclass = await self._reclassify(command)
                self._unit_of_work.commit()
            except BaseException:
                self._unit_of_work.rollback()
                raise
            return self._result(existing, reclass, already_applied=True)

        resolution = ReviewAccountingResolution(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=command.expected_version,
            treatment_type=command.treatment_type,
            expense_account_id=account.id,
            expense_category=command.expense_category,
            approved_by=command.approved_by,
            note=command.note,
        )
        try:
            created = self._accounting_resolution_writer.create_accounting_resolution(resolution)
            self._unit_of_work.commit()
            reclass = await self._reclassify(command)
            self._unit_of_work.commit()
        except BaseException:
            self._unit_of_work.rollback()
            raise
        return self._result(created, reclass, already_applied=False)

    # ------------------------------------------------------------------ eligibility

    def _require_eligible(self, review) -> None:
        from app.application.workbench.dto import ReviewStatus

        if review.status is not ReviewStatus.PENDING_REVIEW:
            raise ReviewStateConflictError("The review is not pending review.")
        if not _has_operating_expense_reason(review.review_reasons):
            raise AccountingResolutionEligibilityError(
                "The review does not currently carry an operating-expense-shaped reason; there is nothing to remediate."
            )

    def _require_eligible_account(self, command: SubmitReviewAccountingResolutionCommand) -> ExpenseAccountCandidate:
        try:
            account = self._expense_account_reader.find_eligible_by_id(
                company_id=command.company_id,
                account_id=command.expense_account_id,
            )
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001 - translated to a safe workflow error
            raise OperatingExpenseMappingWorkflowError(SAFE_ACCOUNTING_RESOLUTION_ERROR) from exc
        if account is None:
            raise OperatingExpenseMappingAccountInvalidError(
                "The selected expense account does not exist, is not an eligible operating-expense "
                "account, or is not scoped to this company."
            )
        return account

    # ------------------------------------------------------------------ reclassification

    async def _reclassify(self, command: SubmitReviewAccountingResolutionCommand):
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
            raise ReviewPersistenceError(SAFE_ACCOUNTING_RESOLUTION_ERROR) from exc

    def _result(
        self,
        resolution: ReviewAccountingResolution,
        reclass,
        *,
        already_applied: bool,
    ) -> ReviewAccountingResolutionSubmissionResult:
        still_required = _has_operating_expense_reason(reclass.new_review_reasons)
        status = (
            AccountingResolutionStatus.REMEDIATION_INCOMPLETE if still_required else AccountingResolutionStatus.RESOLVED
        )
        return ReviewAccountingResolutionSubmissionResult(
            review_id=resolution.review_id,
            company_id=resolution.company_id,
            status=status,
            previous_version=reclass.from_version,
            current_version=reclass.to_version,
            current_workflow=reclass.new_workflow,
            current_review_reasons=reclass.new_review_reasons,
            treatment_type=resolution.treatment_type,
            expense_account_id=resolution.expense_account_id,
            expense_category=resolution.expense_category,
            reclassified=bool(reclass.changed),
            already_applied=already_applied,
            safe_message=(
                "Accounting resolution recorded; the review was reclassified."
                if status is AccountingResolutionStatus.RESOLVED
                else (
                    "The accounting resolution was recorded and the review reclassified, but the review "
                    "still carries an operating-expense-shaped reason. Another blocker may remain."
                )
            ),
        )

    # ------------------------------------------------------------------ resume / idempotency

    def _resume_if_already_applied(
        self,
        command: SubmitReviewAccountingResolutionCommand,
        review,
    ) -> ReviewAccountingResolutionSubmissionResult | None:
        """The review already advanced past ``expected_version``.

        A retry of the *exact same* logical request is recognized and reported
        truthfully from the review's current, already-committed state -- never a
        duplicate resolution row, never a duplicate reclassification. Anything else
        (a genuinely stale version, or a materially different request) still fails
        closed as a version conflict.
        """

        existing = self._accounting_resolution_writer.find_accounting_resolution(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=command.expected_version,
        )
        if existing is None or not _matches(existing, command):
            return None
        still_required = _has_operating_expense_reason(review.review_reasons)
        status = (
            AccountingResolutionStatus.REMEDIATION_INCOMPLETE if still_required else AccountingResolutionStatus.RESOLVED
        )
        return ReviewAccountingResolutionSubmissionResult(
            review_id=command.review_id,
            company_id=command.company_id,
            status=status,
            previous_version=command.expected_version,
            current_version=review.version,
            current_workflow=review.workflow,
            current_review_reasons=review.review_reasons,
            treatment_type=existing.treatment_type,
            expense_account_id=existing.expense_account_id,
            expense_category=existing.expense_category,
            reclassified=True,
            already_applied=True,
            safe_message="This accounting resolution was already applied.",
        )


def _matches(existing: ReviewAccountingResolution, command: SubmitReviewAccountingResolutionCommand) -> bool:
    return (
        existing.treatment_type == command.treatment_type
        and existing.expense_account_id == command.expense_account_id
        and existing.expense_category == command.expense_category
    )


def _has_operating_expense_reason(reasons: tuple[ManualReviewReason, ...]) -> bool:
    return any(
        reason.code
        in (
            ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED,
            ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_AMBIGUOUS,
        )
        for reason in reasons
    )


__all__ = ["AccountingResolutionReclassifier", "SubmitReviewAccountingResolutionUseCase"]
