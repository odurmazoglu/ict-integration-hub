from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.application.execution.ports import AcceptedReviewDecisionReader, ExecutionSourceInvoiceReader
from app.application.services.unit_of_work import UnitOfWork
from app.application.workbench.dto import ReviewDecisionType
from app.application.workbench.ports import ReviewQueueReader
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.write_authorization import (
    WriteAuthorizationOperationType,
    WriteAuthorizationRecord,
    WriteAuthorizationRepository,
    WriteAuthorizationScopeMismatchError,
)
from app.application.workflow import WorkflowType

WRITE_AUTHORIZATION_TTL = timedelta(minutes=15)


class CreateWriteAuthorizationUseCase:
    def __init__(
        self,
        *,
        review_reader: ReviewQueueReader,
        accepted_decision_reader: AcceptedReviewDecisionReader,
        source_invoice_reader: ExecutionSourceInvoiceReader,
        repository: WriteAuthorizationRepository,
        unit_of_work: UnitOfWork,
    ) -> None:
        self._review_reader = review_reader
        self._accepted_decision_reader = accepted_decision_reader
        self._source_invoice_reader = source_invoice_reader
        self._repository = repository
        self._unit_of_work = unit_of_work

    def execute(
        self,
        *,
        company_id: int,
        review_id: str,
        decision_version: int,
        operation_type: WriteAuthorizationOperationType,
        authorized_by: str,
        justification: str | None = None,
    ) -> WriteAuthorizationRecord:
        try:
            review = self._review_reader.get_review_item(ReviewDetailQuery(review_id=review_id, company_id=company_id))
            if review.version != decision_version:
                raise WriteAuthorizationScopeMismatchError(
                    "Authorization must target the current accepted decision version."
                )
            if operation_type is not WriteAuthorizationOperationType.EXECUTE_VENDOR_BILL:
                raise WriteAuthorizationScopeMismatchError("Only Vendor Bill runtime authorization is supported.")
            if not authorized_by.strip():
                raise WriteAuthorizationScopeMismatchError("Authorization actor is required.")
            decision = self._accepted_decision_reader.get_accepted_decision(
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
            )
            if (
                decision.decision_type is not ReviewDecisionType.SELECT_WORKFLOW
                or decision.selected_workflow is not WorkflowType.VENDOR_BILL
                or (
                    decision.business_context_allocations is not None
                    and decision.business_context_allocations.allocations
                )
            ):
                raise WriteAuthorizationScopeMismatchError(
                    "Only accepted direct Vendor Bill decisions may be authorized."
                )
            # Read existing immutable Stage-2 evidence; never capture/reconstruct it.
            self._source_invoice_reader.get_source_invoice(
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
            )
            record = self._repository.create(
                authorization_id=str(uuid4()),
                company_id=company_id,
                review_id=review_id,
                operation_type=operation_type,
                target_version=decision_version,
                authorized_by=authorized_by,
                expires_at=datetime.now(UTC) + WRITE_AUTHORIZATION_TTL,
                justification=justification,
            )
            self._unit_of_work.commit()
            return record
        except Exception:
            self._unit_of_work.rollback()
            raise


class ListWriteAuthorizationsUseCase:
    def __init__(self, *, repository: WriteAuthorizationRepository, review_reader: ReviewQueueReader) -> None:
        self._repository = repository
        self._review_reader = review_reader

    def execute(self, *, company_id: int, review_id: str) -> tuple[WriteAuthorizationRecord, ...]:
        self._review_reader.get_review_item(ReviewDetailQuery(review_id=review_id, company_id=company_id))
        return self._repository.list_for_review(review_id=review_id, company_id=company_id)


class RevokeWriteAuthorizationUseCase:
    def __init__(self, *, repository: WriteAuthorizationRepository, unit_of_work: UnitOfWork) -> None:
        self._repository = repository
        self._unit_of_work = unit_of_work

    def execute(
        self, *, company_id: int, review_id: str, authorization_id: str, revoked_by: str
    ) -> WriteAuthorizationRecord:
        try:
            if not revoked_by.strip():
                raise WriteAuthorizationScopeMismatchError("Revocation actor is required.")
            record = self._repository.revoke(
                authorization_id=authorization_id,
                company_id=company_id,
                review_id=review_id,
                revoked_by=revoked_by,
            )
            self._unit_of_work.commit()
            return record
        except Exception:
            self._unit_of_work.rollback()
            raise
