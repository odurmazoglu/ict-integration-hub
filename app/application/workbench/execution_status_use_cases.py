"""P0-PROD-12A: compose the operator execution/recovery status view for one review."""

from __future__ import annotations

from app.application.execution.contracts import AcceptedReviewDecision, ExecutionArtifact, ExecutionStepType
from app.application.execution.exceptions import ExecutionSourceInvoiceNotFoundError
from app.application.execution.ports import (
    AcceptedReviewDecisionReader,
    ExecutionSourceInvoiceReader,
    WorkbenchExecutionSnapshotReader,
)
from app.application.execution.runtime import ExecutionState
from app.application.workbench.exceptions import ReviewNotFoundError
from app.application.workbench.execution_status import (
    WorkbenchDecisionStatus,
    WorkbenchEvidenceStatus,
    WorkbenchExecutionAuthorizationStatus,
    WorkbenchExecutionStatus,
    WorkbenchExecutionSummary,
    WorkbenchRecoveryStatus,
)
from app.application.workbench.ports import ReviewExecutionEvidenceReader, ReviewQueueReader
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.write_authorization import (
    WriteAuthorizationOperationType,
    WriteAuthorizationRepository,
)


class GetWorkbenchExecutionStatusUseCase:
    """Read-only composition of execution/retry/artifact/evidence/authorization status.

    Every dependency here is an existing read-only reader/repository already
    used elsewhere in the application (accepted decisions, Stage-1/Stage-2
    execution evidence, the execution runtime's own snapshot model, narrow
    write authorizations). This use case adds no new persisted state and
    performs no writes.
    """

    def __init__(
        self,
        *,
        review_reader: ReviewQueueReader,
        accepted_decision_reader: AcceptedReviewDecisionReader,
        execution_snapshot_reader: WorkbenchExecutionSnapshotReader,
        stage_one_evidence_reader: ReviewExecutionEvidenceReader,
        stage_two_evidence_reader: ExecutionSourceInvoiceReader,
        write_authorization_repository: WriteAuthorizationRepository,
    ) -> None:
        self._review_reader = review_reader
        self._accepted_decision_reader = accepted_decision_reader
        self._execution_snapshot_reader = execution_snapshot_reader
        self._stage_one_evidence_reader = stage_one_evidence_reader
        self._stage_two_evidence_reader = stage_two_evidence_reader
        self._write_authorization_repository = write_authorization_repository

    def execute(self, *, review_id: str, company_id: int) -> WorkbenchExecutionStatus:
        # Company-scoped existence check first, exactly like every other narrow
        # Workbench status endpoint (see GetOneOffVendorRetirementUseCase) --
        # this alone fails closed for a wrong-company or unknown review_id.
        review = self._review_reader.get_review_item(ReviewDetailQuery(review_id=review_id, company_id=company_id))

        decision = self._accepted_decision(review_id=review_id, company_id=company_id, decision_version=review.version)

        snapshot = self._execution_snapshot_reader.find_latest_snapshot_for_review(
            review_id=review_id, company_id=company_id
        )

        evidence = self._evidence_status(
            review_id=review_id,
            company_id=company_id,
            decision_version=decision.decision_version if decision is not None else None,
            fallback_review_version=review.version,
        )

        execution_summary: WorkbenchExecutionSummary | None = None
        artifacts: tuple[ExecutionArtifact, ...] = ()
        authorization: WorkbenchExecutionAuthorizationStatus | None = None

        if snapshot is not None:
            vendor_bill_step = next(
                (step for step in snapshot.steps if step.step_type is ExecutionStepType.VENDOR_BILL),
                None,
            )
            retry_count = vendor_bill_step.retry_count if vendor_bill_step is not None else 0
            max_attempts = snapshot.retry_policy.max_attempts
            remaining_attempts = max(0, max_attempts - retry_count)
            retry_possible = snapshot.state is ExecutionState.WAITING_RETRY and remaining_attempts > 0

            execution_summary = WorkbenchExecutionSummary(
                execution_id=snapshot.execution_id,
                mode=snapshot.mode,
                state=snapshot.state,
                retry_count=retry_count,
                max_attempts=max_attempts,
                remaining_attempts=remaining_attempts,
                retry_possible=retry_possible,
            )

            for step in snapshot.steps:
                if step.last_result is not None:
                    artifacts = artifacts + step.last_result.produced_artifacts

            authorization = self._authorization_for_execution(
                review_id=review_id,
                company_id=company_id,
                execution_id=snapshot.execution_id,
            )

        recovery = WorkbenchRecoveryStatus(
            execution_completed=snapshot is not None and snapshot.state is ExecutionState.COMPLETED,
            waiting_retry=snapshot is not None and snapshot.state is ExecutionState.WAITING_RETRY,
            remaining_attempts=execution_summary.remaining_attempts if execution_summary is not None else 0,
        )

        return WorkbenchExecutionStatus(
            review_id=review.review_id,
            company_id=company_id,
            review_version=review.version,
            review_status=review.status,
            decision=(
                WorkbenchDecisionStatus(
                    decision_id=decision.decision_id,
                    decision_version=decision.decision_version,
                    selected_workflow=decision.selected_workflow,
                )
                if decision is not None
                else None
            ),
            execution=execution_summary,
            failure=snapshot.failure if snapshot is not None else None,
            artifacts=artifacts,
            evidence=evidence,
            authorization=authorization,
            recovery=recovery,
        )

    def _accepted_decision(
        self, *, review_id: str, company_id: int, decision_version: int
    ) -> AcceptedReviewDecision | None:
        try:
            return self._accepted_decision_reader.get_accepted_decision(
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
            )
        except ReviewNotFoundError:
            # No decision has ever been submitted at the review's current
            # version -- a normal, non-error state for a fresh/undecided
            # review (P0-PROD-12A section D).
            return None

    def _evidence_status(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int | None,
        fallback_review_version: int,
    ) -> WorkbenchEvidenceStatus:
        # Stage-1 evidence is keyed by the review VERSION a decision was (or
        # would be) submitted against, i.e. review_version_before == decision_version - 1.
        # With no decision yet, best-effort check the review's current version
        # in case reclassification has already captured evidence pre-decision.
        stage_one_expected_version = decision_version - 1 if decision_version is not None else fallback_review_version
        stage_one_present = False
        stage_one_version: int | None = None
        if stage_one_expected_version > 0:
            try:
                self._stage_one_evidence_reader.get_evidence(
                    review_id=review_id,
                    company_id=company_id,
                    expected_version=stage_one_expected_version,
                )
                stage_one_present = True
                stage_one_version = stage_one_expected_version
            except ExecutionSourceInvoiceNotFoundError:
                stage_one_present = False

        stage_two_present = False
        stage_two_decision_version: int | None = None
        if decision_version is not None:
            try:
                self._stage_two_evidence_reader.get_source_invoice(
                    review_id=review_id,
                    company_id=company_id,
                    decision_version=decision_version,
                )
                stage_two_present = True
                stage_two_decision_version = decision_version
            except ExecutionSourceInvoiceNotFoundError:
                stage_two_present = False

        return WorkbenchEvidenceStatus(
            stage_one_present=stage_one_present,
            stage_one_review_version=stage_one_version,
            stage_two_present=stage_two_present,
            stage_two_decision_version=stage_two_decision_version,
        )

    def _authorization_for_execution(
        self, *, review_id: str, company_id: int, execution_id: str
    ) -> WorkbenchExecutionAuthorizationStatus | None:
        records = self._write_authorization_repository.list_for_review(review_id=review_id, company_id=company_id)
        # list_for_review is already ordered created_at desc, so the first
        # provable match is the most recent one -- relevant when TTL expiry
        # forced a fresh authorization to be issued and consumed by the same
        # (deterministic) execution_id on a later retry (P0-PROD-10G shape).
        for record in records:
            if (
                record.operation_type is WriteAuthorizationOperationType.EXECUTE_VENDOR_BILL
                and record.consumed_by_execution_id == execution_id
            ):
                return WorkbenchExecutionAuthorizationStatus(
                    authorization_id=record.authorization_id,
                    operation_type=record.operation_type,
                    target_version=record.target_version,
                    status=record.status,
                    use_count=record.use_count,
                    is_expired=record.is_expired,
                    consumed_by_execution_id=record.consumed_by_execution_id,
                )
        return None
