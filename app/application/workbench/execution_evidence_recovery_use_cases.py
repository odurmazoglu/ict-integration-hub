"""Review-scoped Stage-1 execution-evidence recovery/repair orchestration (P0-PROD-15Z).

Repairs *derived* ``WorkbenchReviewExecutionEvidence`` only, for a review whose
business state (supplier resolution / purchase purpose / accounting resolution) is
already accepted and whose effective reasons/workflow are already exactly what the
review currently shows -- never a generic reclassification, never a way to make an
unresolved review look ready. See
``app.application.workbench.execution_evidence_recovery`` for the module docstring
with the full motivation and ``app.application.use_cases.effective_decision`` for
the shared effective-decision computation this reuses verbatim.
"""

from __future__ import annotations

from app.application.exceptions import ApplicationError
from app.application.services import UnitOfWork
from app.application.use_cases.effective_decision import EffectiveDecision, EffectiveDecisionResolver
from app.application.use_cases.review_classification_outcome import build_review_execution_evidence
from app.application.workbench.evidence import ReviewExecutionEvidence
from app.application.workbench.exceptions import (
    ExecutionEvidenceRecoveryBuildError,
    ExecutionEvidenceRecoveryConflictError,
    ExecutionEvidenceRecoveryEligibilityError,
    ExecutionEvidenceRecoveryMismatchError,
    ExecutionEvidenceRecoverySourceMissingError,
    ReviewNotFoundError,
    ReviewPersistenceError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
    WorkbenchContractError,
)
from app.application.workbench.execution_evidence_recovery import (
    RebuildExecutionEvidenceCommand,
    RebuildExecutionEvidenceResult,
)
from app.application.workbench.ports import ReviewExecutionEvidenceRecoveryWriter, ReviewQueueReader
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workflow import WorkflowType
from app.matching import PartnerMatchStatus

SAFE_EXECUTION_EVIDENCE_RECOVERY_ERROR = "Execution-evidence recovery failed."


def _recovery_idempotency_key(command: RebuildExecutionEvidenceCommand) -> str:
    # The recommendation/manual-review strategies never persist or write this key;
    # it exists only to satisfy the ImportInvoiceCommand contract the resolver's
    # underlying re-decide step uses.
    return f"execution-evidence-recovery:{command.company_id}:{command.review_id}:{command.expected_version}"


class RebuildReviewExecutionEvidenceUseCase:
    """Application boundary for one review-scoped execution-evidence recovery."""

    def __init__(
        self,
        *,
        review_reader: ReviewQueueReader,
        resolver: EffectiveDecisionResolver,
        execution_evidence_writer: ReviewExecutionEvidenceRecoveryWriter,
        unit_of_work: UnitOfWork,
    ) -> None:
        self._review_reader = review_reader
        self._resolver = resolver
        self._execution_evidence_writer = execution_evidence_writer
        self._unit_of_work = unit_of_work

    async def execute(self, command: RebuildExecutionEvidenceCommand) -> RebuildExecutionEvidenceResult:
        if not isinstance(command, RebuildExecutionEvidenceCommand):
            raise WorkbenchContractError("A canonical RebuildExecutionEvidenceCommand is required.")

        review = self._review_reader.get_review_item(
            ReviewDetailQuery(review_id=command.review_id, company_id=command.company_id)
        )
        self._require_eligible(review, command)

        effective = await self._resolve(command)
        self._require_recomputation_matches_persisted_state(effective, review)

        execution_evidence = build_review_execution_evidence(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=command.expected_version,
            invoice=effective.source.invoice,
            decision_result=effective.execution_decision_result,
        )
        if execution_evidence is None:
            raise ExecutionEvidenceRecoveryBuildError(
                "The recomputed effective decision cannot produce a valid Stage-1 execution-evidence snapshot."
            )

        existing = self._execution_evidence_writer.find_execution_evidence(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=command.expected_version,
        )
        if existing is not None:
            if existing != execution_evidence:
                raise ExecutionEvidenceRecoveryConflictError(
                    "Existing execution evidence for this review version conflicts with the freshly "
                    "recomputed evidence; recovery never overwrites existing evidence."
                )
            return self._result(existing, already_applied=True)

        try:
            created = self._execution_evidence_writer.create_execution_evidence_for_current_version(
                review_id=command.review_id,
                company_id=command.company_id,
                expected_version=command.expected_version,
                evidence=execution_evidence,
            )
            self._unit_of_work.commit()
        except BaseException:
            self._unit_of_work.rollback()
            raise
        return self._result(created, already_applied=False)

    async def _resolve(self, command: RebuildExecutionEvidenceCommand) -> EffectiveDecision:
        try:
            return await self._resolver.resolve(
                review_id=command.review_id,
                company_id=command.company_id,
                idempotency_key=_recovery_idempotency_key(command),
            )
        except ReviewNotFoundError as exc:
            # The review item itself was already confirmed to exist above; a
            # ReviewNotFoundError here can only come from the immutable source
            # invoice evidence lookup.
            raise ExecutionEvidenceRecoverySourceMissingError(
                "Immutable source invoice evidence is missing for this review; execution evidence cannot be recomputed."
            ) from exc
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001 - translated to a safe application error
            raise ReviewPersistenceError(SAFE_EXECUTION_EVIDENCE_RECOVERY_ERROR) from exc

    def _require_eligible(self, review, command: RebuildExecutionEvidenceCommand) -> None:
        from app.application.workbench.dto import ReviewStatus

        if review.version != command.expected_version:
            raise ReviewVersionConflictError("The review version does not match expected_version.")
        if review.status is not ReviewStatus.PENDING_REVIEW:
            raise ReviewStateConflictError("The review is not pending review.")
        if review.workflow is not WorkflowType.VENDOR_BILL:
            raise ExecutionEvidenceRecoveryEligibilityError(
                "The review's current workflow is not execution-capable; recovery only applies to a "
                "vendor_bill-workflow review."
            )
        if review.review_reasons:
            raise ExecutionEvidenceRecoveryEligibilityError(
                "The review still carries an actionable manual-review reason; recovery never makes an "
                "unresolved review ready."
            )

    def _require_recomputation_matches_persisted_state(self, effective: EffectiveDecision, review) -> None:
        if effective.effective_workflow is not review.workflow:
            raise ExecutionEvidenceRecoveryMismatchError(
                "Recomputing this review's effective classification right now no longer reproduces its "
                "persisted current workflow."
            )
        persisted_codes = {reason.code for reason in review.review_reasons}
        effective_codes = {reason.code for reason in effective.effective_review_reasons}
        if persisted_codes != effective_codes:
            raise ExecutionEvidenceRecoveryMismatchError(
                "Recomputing this review's effective classification right now no longer reproduces its "
                "persisted current reasons."
            )

    def _result(self, evidence: ReviewExecutionEvidence, *, already_applied: bool) -> RebuildExecutionEvidenceResult:
        partner_match = evidence.partner_match
        partner_id = (
            partner_match.partner_id
            if partner_match is not None and partner_match.status is PartnerMatchStatus.MATCHED
            else None
        )
        operating_expense_match = evidence.operating_expense_match
        return RebuildExecutionEvidenceResult(
            review_id=evidence.review_id,
            company_id=evidence.company_id,
            review_version=evidence.review_version,
            already_applied=already_applied,
            partner_id=partner_id,
            expense_account_id=(
                operating_expense_match.expense_account_id if operating_expense_match is not None else None
            ),
            expense_category=(
                operating_expense_match.expense_category if operating_expense_match is not None else None
            ),
            safe_message=(
                "Execution evidence already existed for this review version; nothing was created."
                if already_applied
                else "Execution evidence was recomputed and persisted for this review's current version."
            ),
        )


__all__ = ["RebuildReviewExecutionEvidenceUseCase"]
