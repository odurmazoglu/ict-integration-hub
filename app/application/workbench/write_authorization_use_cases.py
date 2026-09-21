from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.application.execution.ports import AcceptedReviewDecisionReader, ExecutionSourceInvoiceReader
from app.application.services.unit_of_work import UnitOfWork
from app.application.workbench.dto import ReviewDecisionType
from app.application.workbench.ports import OneOffVendorRetirementWriter, ReviewQueueReader
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
    """Issues one narrow, short-lived, single-use write authorization.

    Pre-validation is operation-specific (see ``_ensure_scope_is_authorizable``),
    but issuance itself -- the actual persisted row, TTL, single-use/locking
    semantics -- is identical for every operation type (P0-PROD-09D1/09F): this
    use case never grants a broader capability than "attempt exactly this one
    write, for this exact review/version, once, within 15 minutes."
    """

    def __init__(
        self,
        *,
        review_reader: ReviewQueueReader,
        accepted_decision_reader: AcceptedReviewDecisionReader,
        source_invoice_reader: ExecutionSourceInvoiceReader,
        retirement_reader: OneOffVendorRetirementWriter,
        repository: WriteAuthorizationRepository,
        unit_of_work: UnitOfWork,
    ) -> None:
        self._review_reader = review_reader
        self._accepted_decision_reader = accepted_decision_reader
        self._source_invoice_reader = source_invoice_reader
        self._retirement_reader = retirement_reader
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
            if not authorized_by.strip():
                raise WriteAuthorizationScopeMismatchError("Authorization actor is required.")
            self._ensure_scope_is_authorizable(
                company_id=company_id,
                review_id=review_id,
                target_version=decision_version,
                operation_type=operation_type,
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

    def _ensure_scope_is_authorizable(
        self,
        *,
        company_id: int,
        review_id: str,
        target_version: int,
        operation_type: WriteAuthorizationOperationType,
    ) -> None:
        if operation_type is WriteAuthorizationOperationType.ONE_OFF_VENDOR_ARCHIVE:
            # Targets an already-persisted retirement row's OWN version, which by
            # design is usually behind the review's current version by the time
            # recovery is needed -- never the review's current version. Consumption
            # (claim_and_consume) re-checks this exact same distinction.
            retirement = self._retirement_reader.find(
                review_id=review_id, company_id=company_id, review_version=target_version
            )
            if retirement is None:
                raise WriteAuthorizationScopeMismatchError(
                    "No ONE_OFF_VENDOR retirement exists for this review version."
                )
            return

        review = self._review_reader.get_review_item(ReviewDetailQuery(review_id=review_id, company_id=company_id))
        if review.version != target_version:
            raise WriteAuthorizationScopeMismatchError("Authorization must target the review's current version.")

        if operation_type is WriteAuthorizationOperationType.EXECUTE_VENDOR_BILL:
            decision = self._accepted_decision_reader.get_accepted_decision(
                review_id=review_id,
                company_id=company_id,
                decision_version=target_version,
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
                decision_version=target_version,
            )
            return

        # CREATE_PERMANENT_SUPPLIER / ONE_OFF_VENDOR_SUPPLIER / CREATE_NEW_PRODUCT: the
        # review existing at exactly this version is the whole precondition -- the
        # owning use case (ResolveWorkbenchSupplierUseCase / CreateNewProductUseCase)
        # still independently re-validates its own operation-specific eligibility
        # (SUPPLIER_NOT_FOUND/mode rules, or PENDING_REVIEW + PRODUCT_NOT_FOUND +
        # resolved supplier) at consumption time; this issuance check never
        # duplicates or anticipates that -- an authorization can be issued and still
        # be correctly refused at use time if the underlying review state does not
        # qualify.


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
