from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.exceptions import ApplicationError
from app.application.execution.contracts import AcceptedReviewDecision, ExecutionSourceInvoice
from app.application.execution.exceptions import ExecutionSourceInvoiceError, ExecutionSourceInvoiceIntegrityError
from app.application.workbench.allocations import (
    AllocationCompleteness,
    BusinessContextAllocation,
    BusinessContextAllocationSet,
    BusinessContextAllocationType,
)
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.dto import (
    BusinessContextDecision,
    LineResolution,
    ReviewDecisionAcknowledgement,
    ReviewDecisionType,
    ReviewItem,
    ReviewQueueResult,
    ReviewStatus,
    TaxResolution,
)
from app.application.workbench.evidence import ReviewExecutionBillingEvidence, ReviewExecutionEvidence
from app.application.workbench.exceptions import (
    ReviewDataIntegrityError,
    ReviewDecisionDataIntegrityError,
    ReviewDecisionError,
    ReviewDecisionIdempotencyConflictError,
    ReviewIdempotencyConflictError,
    ReviewNotFoundError,
    ReviewPersistenceError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
    WorkbenchContractError,
)
from app.application.workbench.queries import ReviewDetailQuery, ReviewQueueQuery
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.models.execution_source_invoice_evidence import ExecutionSourceInvoiceEvidence
from app.models.workbench_review_billing_evidence import WorkbenchReviewBillingEvidence
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import REVIEW_AMOUNT_PRECISION, REVIEW_AMOUNT_SCALE, WorkbenchReviewItem
from app.persistence.execution_source_invoice_reader import (
    deserialize_execution_source_invoice_payload,
    serialize_execution_source_invoice,
    serialize_execution_source_invoice_payload,
)
from app.persistence.review_billing_evidence_reader import (
    REVIEW_BILLING_EVIDENCE_SCHEMA_VERSION,
    deserialize_billing_instruction_payload,
    serialize_billing_instruction_payload,
)

SAFE_PERSISTENCE_ERROR = "Review persistence operation failed."
SAFE_DECISION_ERROR = "Review decision persistence operation failed."
REVIEW_AMOUNT_INTEGER_DIGITS = REVIEW_AMOUNT_PRECISION - REVIEW_AMOUNT_SCALE
REVIEW_EVIDENCE_SCHEMA_VERSION = 1


class SqlAlchemyReviewRepository:
    """SQLAlchemy adapter for idempotent review item creation and queue reads."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_review_item(self, item: ReviewItem, *, company_id: int, idempotency_key: str) -> ReviewItem:
        _validate_create_request(item, company_id=company_id, idempotency_key=idempotency_key)
        try:
            existing = self._find_by_idempotency_key(company_id=company_id, idempotency_key=idempotency_key)
            if existing is not None:
                return self._return_existing_or_raise_conflict(existing, item, company_id=company_id)

            record = _model_from_review_item(item, company_id=company_id, idempotency_key=idempotency_key)
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
                self._session.refresh(record)
            return _review_item_from_model(record)
        except IntegrityError as exc:
            return self._handle_create_integrity_error(
                item,
                company_id=company_id,
                idempotency_key=idempotency_key,
                exc=exc,
            )
        except ReviewPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from exc

    def create_review_item_with_execution_evidence(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        evidence: ReviewExecutionEvidence,
    ) -> ReviewItem:
        _validate_create_request(item, company_id=company_id, idempotency_key=idempotency_key)
        _validate_review_evidence_linkage(item=item, company_id=company_id, evidence=evidence)
        try:
            existing = self._find_by_idempotency_key(company_id=company_id, idempotency_key=idempotency_key)
            if existing is not None:
                existing_item = self._return_existing_or_raise_conflict(existing, item, company_id=company_id)
                self._return_existing_review_evidence_or_raise_conflict(evidence)
                return existing_item

            record = _model_from_review_item(item, company_id=company_id, idempotency_key=idempotency_key)
            evidence_record = _evidence_model_from_review_evidence(evidence)
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
                self._session.add(evidence_record)
                self._session.flush()
                self._session.refresh(record)
            return _review_item_from_model(record)
        except IntegrityError as exc:
            return self._handle_create_with_evidence_integrity_error(
                item,
                company_id=company_id,
                idempotency_key=idempotency_key,
                evidence=evidence,
                exc=exc,
            )
        except ReviewPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from exc

    def create_review_item_with_billing_evidence(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        billing_evidence: tuple[ReviewExecutionBillingEvidence, ...],
    ) -> ReviewItem:
        _validate_create_request(item, company_id=company_id, idempotency_key=idempotency_key)
        _validate_review_billing_evidence_linkage(item=item, company_id=company_id, billing_evidence=billing_evidence)
        try:
            existing = self._find_by_idempotency_key(company_id=company_id, idempotency_key=idempotency_key)
            if existing is not None:
                existing_item = self._return_existing_or_raise_conflict(existing, item, company_id=company_id)
                self._return_existing_review_billing_evidence_or_raise_conflict(billing_evidence)
                return existing_item

            record = _model_from_review_item(item, company_id=company_id, idempotency_key=idempotency_key)
            billing_records = tuple(_billing_evidence_model(evidence) for evidence in billing_evidence)
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
                self._session.add_all(billing_records)
                self._session.flush()
                self._session.refresh(record)
            return _review_item_from_model(record)
        except IntegrityError as exc:
            return self._handle_create_with_billing_evidence_integrity_error(
                item,
                company_id=company_id,
                idempotency_key=idempotency_key,
                billing_evidence=billing_evidence,
                exc=exc,
            )
        except ReviewPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from exc

    def create_review_item_with_execution_and_billing_evidence(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        evidence: ReviewExecutionEvidence,
        billing_evidence: tuple[ReviewExecutionBillingEvidence, ...],
    ) -> ReviewItem:
        _validate_create_request(item, company_id=company_id, idempotency_key=idempotency_key)
        _validate_review_evidence_linkage(item=item, company_id=company_id, evidence=evidence)
        _validate_review_billing_evidence_linkage(item=item, company_id=company_id, billing_evidence=billing_evidence)
        try:
            existing = self._find_by_idempotency_key(company_id=company_id, idempotency_key=idempotency_key)
            if existing is not None:
                existing_item = self._return_existing_or_raise_conflict(existing, item, company_id=company_id)
                self._return_existing_review_evidence_or_raise_conflict(evidence)
                self._return_existing_review_billing_evidence_or_raise_conflict(billing_evidence)
                return existing_item

            record = _model_from_review_item(item, company_id=company_id, idempotency_key=idempotency_key)
            evidence_record = _evidence_model_from_review_evidence(evidence)
            billing_records = tuple(_billing_evidence_model(billing) for billing in billing_evidence)
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
                self._session.add(evidence_record)
                self._session.add_all(billing_records)
                self._session.flush()
                self._session.refresh(record)
            return _review_item_from_model(record)
        except IntegrityError as exc:
            return self._handle_create_with_billing_evidence_integrity_error(
                item,
                company_id=company_id,
                idempotency_key=idempotency_key,
                billing_evidence=billing_evidence,
                exc=exc,
            )
        except ReviewPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from exc

    def get_review_billing_evidence(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> tuple[ReviewExecutionBillingEvidence, ...]:
        _validate_evidence_query(review_id=review_id, company_id=company_id, review_version=review_version)
        try:
            records = tuple(
                self._session.scalars(
                    select(WorkbenchReviewBillingEvidence)
                    .where(
                        WorkbenchReviewBillingEvidence.review_id == review_id,
                        WorkbenchReviewBillingEvidence.company_id == company_id,
                        WorkbenchReviewBillingEvidence.review_version == review_version,
                    )
                    .order_by(WorkbenchReviewBillingEvidence.billing_key.asc(), WorkbenchReviewBillingEvidence.id.asc())
                )
            )
            if not records:
                raise ReviewNotFoundError("Review billing evidence was not found.")
            return tuple(_billing_evidence_from_model(record) for record in records)
        except ApplicationError:
            raise
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise ReviewDataIntegrityError("Review billing evidence is invalid.") from exc
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from exc

    def get_review_execution_evidence(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> ReviewExecutionEvidence:
        _validate_evidence_query(review_id=review_id, company_id=company_id, review_version=review_version)
        try:
            records = tuple(
                self._session.scalars(
                    select(WorkbenchReviewExecutionEvidence)
                    .where(
                        WorkbenchReviewExecutionEvidence.review_id == review_id,
                        WorkbenchReviewExecutionEvidence.company_id == company_id,
                        WorkbenchReviewExecutionEvidence.review_version == review_version,
                    )
                    .order_by(WorkbenchReviewExecutionEvidence.id.asc())
                    .limit(2)
                )
            )
            if not records:
                raise ReviewNotFoundError("Review execution evidence was not found.")
            if len(records) > 1:
                raise ReviewDataIntegrityError("Review execution evidence is ambiguous.")
            return _review_evidence_from_model(records[0])
        except ExecutionSourceInvoiceError as exc:
            raise ReviewDataIntegrityError("Review execution evidence is invalid.") from exc
        except ApplicationError:
            raise
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise ReviewDataIntegrityError("Review execution evidence is invalid.") from exc
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from exc

    def list_review_items(self, query: ReviewQueueQuery) -> ReviewQueueResult:
        try:
            filters = _query_filters(query)
            total_count = self._session.scalar(select(func.count()).select_from(WorkbenchReviewItem).where(*filters))
            records = tuple(
                self._session.scalars(
                    select(WorkbenchReviewItem)
                    .where(*filters)
                    .order_by(WorkbenchReviewItem.created_at.asc(), WorkbenchReviewItem.review_id.asc())
                    .offset(query.offset)
                    .limit(query.limit)
                )
            )
            return ReviewQueueResult(
                items=tuple(_review_item_from_model(record) for record in records),
                total_count=int(total_count or 0),
                limit=query.limit,
                offset=query.offset,
            )
        except ReviewPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from exc

    def get_review_item(self, query: ReviewDetailQuery) -> ReviewItem:
        try:
            record = self._session.scalar(
                select(WorkbenchReviewItem).where(
                    WorkbenchReviewItem.review_id == query.review_id,
                    WorkbenchReviewItem.company_id == query.company_id,
                )
            )
            if record is None:
                raise ReviewNotFoundError("Review item was not found.")
            return _review_item_from_model(record)
        except ReviewPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from exc

    def submit_review_decision(self, command: ReviewDecisionCommand) -> ReviewDecisionAcknowledgement:
        if _requires_execution_evidence(command):
            raise ReviewDecisionError("Execution source evidence is required for Vendor Bill decisions.")
        try:
            existing = self._find_decision_by_idempotency_key(
                company_id=command.company_id,
                idempotency_key=command.idempotency_key,
            )
            if existing is not None:
                return self._return_existing_decision_or_raise_conflict(existing, command)

            target_status = _target_status_for_decision(command.decision)
            decision = _decision_model_from_command(
                command,
                decision_id=f"review-decision:{uuid4()}",
            )
            with self._session.begin_nested():
                result = self._session.execute(
                    update(WorkbenchReviewItem)
                    .where(
                        WorkbenchReviewItem.review_id == command.review_id,
                        WorkbenchReviewItem.company_id == command.company_id,
                        WorkbenchReviewItem.status == ReviewStatus.PENDING_REVIEW.value,
                        WorkbenchReviewItem.version == command.expected_version,
                    )
                    .values(
                        status=target_status.value,
                        version=command.expected_version + 1,
                        updated_at=func.now(),
                    )
                    .execution_options(synchronize_session=False)
                )
                if int(result.rowcount or 0) != 1:
                    self._raise_submission_conflict(command)
                self._session.add(decision)
                self._session.flush()
                self._session.refresh(decision)
            return _acknowledgement_from_decision_model(decision)
        except IntegrityError as exc:
            return self._handle_decision_integrity_error(command, exc=exc)
        except ApplicationError:
            self._session.expire_all()
            raise
        except SQLAlchemyError as exc:
            self._session.expire_all()
            raise ReviewDecisionError(SAFE_DECISION_ERROR) from exc

    def submit_review_decision_with_execution_evidence(
        self,
        command: ReviewDecisionCommand,
        evidence: ExecutionSourceInvoice,
    ) -> ReviewDecisionAcknowledgement:
        _validate_execution_evidence_for_command(command, evidence)
        try:
            existing = self._find_decision_by_idempotency_key(
                company_id=command.company_id,
                idempotency_key=command.idempotency_key,
            )
            if existing is not None:
                acknowledgement = self._return_existing_decision_or_raise_conflict(existing, command)
                self._return_existing_evidence_or_raise_conflict(existing, evidence)
                return acknowledgement

            target_status = _target_status_for_decision(command.decision)
            decision = _decision_model_from_command(
                command,
                decision_id=f"review-decision:{uuid4()}",
            )
            with self._session.begin_nested():
                result = self._session.execute(
                    update(WorkbenchReviewItem)
                    .where(
                        WorkbenchReviewItem.review_id == command.review_id,
                        WorkbenchReviewItem.company_id == command.company_id,
                        WorkbenchReviewItem.status == ReviewStatus.PENDING_REVIEW.value,
                        WorkbenchReviewItem.version == command.expected_version,
                    )
                    .values(
                        status=target_status.value,
                        version=command.expected_version + 1,
                        updated_at=func.now(),
                    )
                    .execution_options(synchronize_session=False)
                )
                if int(result.rowcount or 0) != 1:
                    self._raise_submission_conflict(command)
                review_item = self._review_item_for_evidence_capture(command)
                _validate_execution_evidence_for_review_item(review_item, evidence)
                self._session.add(decision)
                self._session.flush()
                self._session.refresh(decision)
                self._add_execution_source_evidence(decision, evidence)
                self._session.flush()
            return _acknowledgement_from_decision_model(decision)
        except IntegrityError as exc:
            return self._handle_decision_with_evidence_integrity_error(command, evidence, exc=exc)
        except ApplicationError:
            self._session.expire_all()
            raise
        except SQLAlchemyError as exc:
            self._session.expire_all()
            raise ReviewDecisionError(SAFE_DECISION_ERROR) from exc

    def get_accepted_decision(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
    ) -> AcceptedReviewDecision:
        _validate_accepted_decision_query(
            review_id=review_id,
            company_id=company_id,
            decision_version=decision_version,
        )
        try:
            records = tuple(
                self._session.scalars(
                    select(WorkbenchReviewDecision)
                    .where(
                        WorkbenchReviewDecision.review_id == review_id,
                        WorkbenchReviewDecision.company_id == company_id,
                        WorkbenchReviewDecision.review_version_after == decision_version,
                    )
                    .order_by(WorkbenchReviewDecision.id.asc())
                    .limit(2)
                )
            )
            if not records:
                raise ReviewNotFoundError("Accepted review decision was not found.")
            if len(records) > 1:
                raise ReviewDecisionDataIntegrityError("Accepted review decision is ambiguous.")
            return _accepted_review_decision_from_model(records[0])
        except ApplicationError:
            raise
        except SQLAlchemyError as exc:
            raise ReviewDecisionError(SAFE_DECISION_ERROR) from exc

    def _find_by_idempotency_key(
        self,
        *,
        company_id: int,
        idempotency_key: str,
    ) -> WorkbenchReviewItem | None:
        return self._session.scalar(
            select(WorkbenchReviewItem).where(
                WorkbenchReviewItem.company_id == company_id,
                WorkbenchReviewItem.idempotency_key == idempotency_key,
            )
        )

    def _return_existing_or_raise_conflict(
        self,
        existing: WorkbenchReviewItem,
        item: ReviewItem,
        *,
        company_id: int,
    ) -> ReviewItem:
        existing_item = _review_item_from_model(existing)
        if _business_fingerprint(existing_item, company_id=company_id) != _business_fingerprint(
            item,
            company_id=company_id,
        ):
            raise ReviewIdempotencyConflictError("Review idempotency key conflicts with an existing review item.")
        return existing_item

    def _handle_create_integrity_error(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        exc: IntegrityError,
    ) -> ReviewItem:
        try:
            existing = self._find_by_idempotency_key(company_id=company_id, idempotency_key=idempotency_key)
        except SQLAlchemyError as lookup_exc:
            raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from lookup_exc
        if existing is not None:
            try:
                return self._return_existing_or_raise_conflict(existing, item, company_id=company_id)
            except ReviewPersistenceError as conflict_exc:
                raise conflict_exc from exc

        try:
            same_review_id = self._session.scalar(
                select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == item.review_id)
            )
        except SQLAlchemyError as lookup_exc:
            raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from lookup_exc
        if same_review_id is not None:
            raise ReviewDataIntegrityError("Review item already exists.") from exc
        raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from exc

    def _handle_create_with_evidence_integrity_error(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        evidence: ReviewExecutionEvidence,
        exc: IntegrityError,
    ) -> ReviewItem:
        try:
            existing = self._find_by_idempotency_key(company_id=company_id, idempotency_key=idempotency_key)
        except SQLAlchemyError as lookup_exc:
            raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from lookup_exc
        if existing is not None:
            try:
                existing_item = self._return_existing_or_raise_conflict(existing, item, company_id=company_id)
                self._return_existing_review_evidence_or_raise_conflict(evidence)
                return existing_item
            except ReviewPersistenceError as conflict_exc:
                raise conflict_exc from exc
        raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from exc

    def _handle_create_with_billing_evidence_integrity_error(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        billing_evidence: tuple[ReviewExecutionBillingEvidence, ...],
        exc: IntegrityError,
    ) -> ReviewItem:
        try:
            existing = self._find_by_idempotency_key(company_id=company_id, idempotency_key=idempotency_key)
        except SQLAlchemyError as lookup_exc:
            raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from lookup_exc
        if existing is not None:
            try:
                existing_item = self._return_existing_or_raise_conflict(existing, item, company_id=company_id)
                self._return_existing_review_billing_evidence_or_raise_conflict(billing_evidence)
                return existing_item
            except ReviewPersistenceError as conflict_exc:
                raise conflict_exc from exc
        raise ReviewPersistenceError(SAFE_PERSISTENCE_ERROR) from exc

    def _return_existing_review_evidence_or_raise_conflict(
        self,
        evidence: ReviewExecutionEvidence,
    ) -> ReviewExecutionEvidence:
        existing = self._find_review_execution_evidence(
            review_id=evidence.review_id,
            company_id=evidence.company_id,
            review_version=evidence.review_version,
        )
        if existing is None:
            raise ReviewDataIntegrityError("Review execution evidence is missing for existing review item.")
        existing_evidence = _review_evidence_from_model(existing)
        if _review_evidence_fingerprint(existing_evidence) != _review_evidence_fingerprint(evidence):
            raise ReviewIdempotencyConflictError("Review execution evidence conflicts with existing review version.")
        return existing_evidence

    def _return_existing_review_billing_evidence_or_raise_conflict(
        self,
        billing_evidence: tuple[ReviewExecutionBillingEvidence, ...],
    ) -> tuple[ReviewExecutionBillingEvidence, ...]:
        review_id, company_id, review_version = _billing_evidence_identity(billing_evidence)
        existing = self._find_all_review_billing_evidence(
            review_id=review_id,
            company_id=company_id,
            review_version=review_version,
        )
        if not existing:
            raise ReviewDataIntegrityError("Review billing evidence is missing for existing review item.")
        existing_evidence = tuple(_billing_evidence_from_model(record) for record in existing)
        if _billing_evidence_fingerprint(existing_evidence) != _billing_evidence_fingerprint(billing_evidence):
            raise ReviewIdempotencyConflictError("Review billing evidence conflicts with existing review version.")
        return existing_evidence

    def _find_review_execution_evidence(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> WorkbenchReviewExecutionEvidence | None:
        return self._session.scalar(
            select(WorkbenchReviewExecutionEvidence).where(
                WorkbenchReviewExecutionEvidence.review_id == review_id,
                WorkbenchReviewExecutionEvidence.company_id == company_id,
                WorkbenchReviewExecutionEvidence.review_version == review_version,
            )
        )

    def _find_all_review_billing_evidence(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> tuple[WorkbenchReviewBillingEvidence, ...]:
        return tuple(
            self._session.scalars(
                select(WorkbenchReviewBillingEvidence)
                .where(
                    WorkbenchReviewBillingEvidence.review_id == review_id,
                    WorkbenchReviewBillingEvidence.company_id == company_id,
                    WorkbenchReviewBillingEvidence.review_version == review_version,
                )
                .order_by(
                    WorkbenchReviewBillingEvidence.billing_key.asc(),
                    WorkbenchReviewBillingEvidence.id.asc(),
                )
            )
        )

    def _find_decision_by_idempotency_key(
        self,
        *,
        company_id: int,
        idempotency_key: str,
    ) -> WorkbenchReviewDecision | None:
        return self._session.scalar(
            select(WorkbenchReviewDecision).where(
                WorkbenchReviewDecision.company_id == company_id,
                WorkbenchReviewDecision.idempotency_key == idempotency_key,
            )
        )

    def _return_existing_decision_or_raise_conflict(
        self,
        existing: WorkbenchReviewDecision,
        command: ReviewDecisionCommand,
    ) -> ReviewDecisionAcknowledgement:
        if _decision_fingerprint_from_model(existing) != _decision_fingerprint(command):
            raise ReviewDecisionIdempotencyConflictError(
                "Review decision idempotency key conflicts with an existing decision."
            )
        return _acknowledgement_from_decision_model(existing)

    def _return_existing_evidence_or_raise_conflict(
        self,
        existing_decision: WorkbenchReviewDecision,
        evidence: ExecutionSourceInvoice,
    ) -> ExecutionSourceInvoiceEvidence:
        record = self._find_execution_evidence_by_decision_id(existing_decision.decision_id)
        if record is None:
            raise ReviewDecisionDataIntegrityError("Execution source evidence is missing for the accepted decision.")
        if _evidence_fingerprint_from_model(record) != _evidence_fingerprint(
            evidence,
            decision_id=existing_decision.decision_id,
        ):
            raise ReviewDecisionIdempotencyConflictError(
                "Execution source evidence conflicts with the accepted decision."
            )
        return record

    def _add_execution_source_evidence(
        self,
        decision: WorkbenchReviewDecision,
        evidence: ExecutionSourceInvoice,
    ) -> None:
        self._session.add(
            ExecutionSourceInvoiceEvidence(
                **serialize_execution_source_invoice(evidence, decision_id=decision.decision_id)
            )
        )

    def _find_execution_evidence_by_decision_id(self, decision_id: str) -> ExecutionSourceInvoiceEvidence | None:
        return self._session.scalar(
            select(ExecutionSourceInvoiceEvidence).where(ExecutionSourceInvoiceEvidence.decision_id == decision_id)
        )

    def _review_item_for_evidence_capture(self, command: ReviewDecisionCommand) -> WorkbenchReviewItem:
        record = self._session.scalar(
            select(WorkbenchReviewItem).where(
                WorkbenchReviewItem.review_id == command.review_id,
                WorkbenchReviewItem.company_id == command.company_id,
            )
        )
        if record is None:
            raise ReviewNotFoundError("Review item was not found.")
        return record

    def _raise_submission_conflict(self, command: ReviewDecisionCommand) -> None:
        try:
            record = self._session.scalar(
                select(WorkbenchReviewItem).where(
                    WorkbenchReviewItem.review_id == command.review_id,
                    WorkbenchReviewItem.company_id == command.company_id,
                )
            )
        except SQLAlchemyError as exc:
            raise ReviewDecisionError(SAFE_DECISION_ERROR) from exc
        if record is None:
            raise ReviewNotFoundError("Review item was not found.")
        if record.status != ReviewStatus.PENDING_REVIEW.value:
            raise ReviewStateConflictError("Review item is no longer pending review.")
        if record.version != command.expected_version:
            raise ReviewVersionConflictError("Review item version does not match expected_version.")
        raise ReviewStateConflictError("Review item could not accept the decision.")

    def _handle_decision_integrity_error(
        self,
        command: ReviewDecisionCommand,
        *,
        exc: IntegrityError,
    ) -> ReviewDecisionAcknowledgement:
        try:
            existing = self._find_decision_by_idempotency_key(
                company_id=command.company_id,
                idempotency_key=command.idempotency_key,
            )
        except SQLAlchemyError as lookup_exc:
            raise ReviewDecisionError(SAFE_DECISION_ERROR) from lookup_exc
        if existing is not None:
            try:
                return self._return_existing_decision_or_raise_conflict(existing, command)
            except ReviewDecisionError as conflict_exc:
                raise conflict_exc from exc
        raise ReviewDecisionError(SAFE_DECISION_ERROR) from exc

    def _handle_decision_with_evidence_integrity_error(
        self,
        command: ReviewDecisionCommand,
        evidence: ExecutionSourceInvoice,
        *,
        exc: IntegrityError,
    ) -> ReviewDecisionAcknowledgement:
        try:
            existing = self._find_decision_by_idempotency_key(
                company_id=command.company_id,
                idempotency_key=command.idempotency_key,
            )
        except SQLAlchemyError as lookup_exc:
            raise ReviewDecisionError(SAFE_DECISION_ERROR) from lookup_exc
        if existing is not None:
            try:
                acknowledgement = self._return_existing_decision_or_raise_conflict(existing, command)
                self._return_existing_evidence_or_raise_conflict(existing, evidence)
                return acknowledgement
            except ReviewDecisionError as conflict_exc:
                raise conflict_exc from exc
        raise ReviewDecisionError(SAFE_DECISION_ERROR) from exc


def _validate_create_request(item: ReviewItem, *, company_id: int, idempotency_key: str) -> None:
    if type(company_id) is not int or company_id <= 0:
        raise WorkbenchContractError("company_id must be positive.")
    if idempotency_key is None or not idempotency_key.strip():
        raise WorkbenchContractError("idempotency_key is required.")
    if item.status is not ReviewStatus.PENDING_REVIEW:
        raise WorkbenchContractError("Only PENDING_REVIEW review items can be created.")
    if item.version != 1:
        raise WorkbenchContractError("New review items must start at version 1.")
    _validate_supported_amount(item.total_amount)


def _validate_accepted_decision_query(*, review_id: str, company_id: int, decision_version: int) -> None:
    if review_id is None or not isinstance(review_id, str) or not review_id.strip():
        raise WorkbenchContractError("review_id is required.")
    if type(company_id) is not int or company_id <= 0:
        raise WorkbenchContractError("company_id must be positive.")
    if type(decision_version) is not int or decision_version <= 0:
        raise WorkbenchContractError("decision_version must be positive.")


def _validate_evidence_query(*, review_id: str, company_id: int, review_version: int) -> None:
    if review_id is None or not isinstance(review_id, str) or not review_id.strip():
        raise WorkbenchContractError("review_id is required.")
    if type(company_id) is not int or company_id <= 0:
        raise WorkbenchContractError("company_id must be positive.")
    if type(review_version) is not int or review_version <= 0:
        raise WorkbenchContractError("review_version must be positive.")


def _validate_review_evidence_linkage(
    *,
    item: ReviewItem,
    company_id: int,
    evidence: ReviewExecutionEvidence,
) -> None:
    if evidence.review_id != item.review_id:
        raise WorkbenchContractError("Evidence review_id must match review item.")
    if evidence.company_id != company_id:
        raise WorkbenchContractError("Evidence company_id must match review item company.")
    if evidence.review_version != item.version:
        raise WorkbenchContractError("Evidence review_version must match review item version.")
    if evidence.source_invoice_id != item.invoice_id:
        raise WorkbenchContractError("Evidence source_invoice_id must match review item invoice_id.")
    for line_result in evidence.product_match.line_results:
        if line_result.result.line_number != line_result.line_number:
            raise WorkbenchContractError("Product match line linkage is invalid.")
    for tax_result in evidence.tax_match.line_results:
        if tax_result.result.company_id is not None and tax_result.result.company_id != company_id:
            raise WorkbenchContractError("Tax mapping company_id must match review item company.")


def _validate_review_billing_evidence_linkage(
    *,
    item: ReviewItem,
    company_id: int,
    billing_evidence: tuple[ReviewExecutionBillingEvidence, ...],
) -> None:
    if not isinstance(billing_evidence, tuple) or not billing_evidence:
        raise WorkbenchContractError("Review billing evidence is required.")
    billing_keys: list[str] = []
    for evidence in billing_evidence:
        if not isinstance(evidence, ReviewExecutionBillingEvidence):
            raise WorkbenchContractError("ReviewExecutionBillingEvidence DTO is required.")
        if evidence.review_id != item.review_id:
            raise WorkbenchContractError("Billing evidence review_id must match review item.")
        if evidence.company_id != company_id:
            raise WorkbenchContractError("Billing evidence company_id must match review item company.")
        if evidence.review_version != item.version:
            raise WorkbenchContractError("Billing evidence review_version must match review item version.")
        billing_keys.append(evidence.billing_instruction.billing_key)
    if len(set(billing_keys)) != len(billing_keys):
        raise WorkbenchContractError("Billing evidence billing_key values must be unique per review version.")


def _validate_execution_evidence_for_command(
    command: ReviewDecisionCommand,
    evidence: ExecutionSourceInvoice,
) -> None:
    if not _requires_execution_evidence(command):
        raise WorkbenchContractError("Execution source evidence is only accepted for Vendor Bill decisions.")
    if not isinstance(evidence, ExecutionSourceInvoice):
        raise WorkbenchContractError("ExecutionSourceInvoice evidence is required.")
    if evidence.review_id != command.review_id:
        raise ExecutionSourceInvoiceIntegrityError("Execution source evidence is invalid.")
    if evidence.company_id != command.company_id:
        raise ExecutionSourceInvoiceIntegrityError("Execution source evidence is invalid.")
    if evidence.decision_version != command.expected_version + 1:
        raise ExecutionSourceInvoiceIntegrityError("Execution source evidence is invalid.")
    invoice_identity = evidence.invoice.header.ettn or evidence.invoice.header.invoice_uuid
    if evidence.source_invoice_id != invoice_identity:
        raise ExecutionSourceInvoiceIntegrityError("Execution source evidence is invalid.")


def _validate_execution_evidence_for_review_item(
    review_item: WorkbenchReviewItem,
    evidence: ExecutionSourceInvoice,
) -> None:
    if review_item.review_id != evidence.review_id:
        raise ExecutionSourceInvoiceIntegrityError("Execution source evidence is invalid.")
    if review_item.company_id != evidence.company_id:
        raise ExecutionSourceInvoiceIntegrityError("Execution source evidence is invalid.")
    if review_item.invoice_id != evidence.source_invoice_id:
        raise ExecutionSourceInvoiceIntegrityError("Execution source evidence is invalid.")


def _requires_execution_evidence(command: ReviewDecisionCommand) -> bool:
    return (
        command.decision is ReviewDecisionType.SELECT_WORKFLOW and command.selected_workflow is WorkflowType.VENDOR_BILL
    )


def _query_filters(query: ReviewQueueQuery) -> list[Any]:
    filters: list[Any] = [
        WorkbenchReviewItem.company_id == query.company_id,
        WorkbenchReviewItem.status == query.status.value,
    ]
    if query.supplier_tax_number is not None:
        filters.append(WorkbenchReviewItem.supplier_tax_number == query.supplier_tax_number)
    if query.workflow is not None:
        filters.append(WorkbenchReviewItem.workflow == query.workflow.value)
    if query.created_from is not None:
        filters.append(WorkbenchReviewItem.created_at >= query.created_from)
    if query.created_to is not None:
        filters.append(WorkbenchReviewItem.created_at <= query.created_to)
    return filters


def _model_from_review_item(
    item: ReviewItem,
    *,
    company_id: int,
    idempotency_key: str,
) -> WorkbenchReviewItem:
    return WorkbenchReviewItem(
        review_id=item.review_id,
        company_id=company_id,
        invoice_id=item.invoice_id,
        invoice_number=item.invoice_number,
        supplier_tax_number=item.supplier_tax_number,
        supplier_name=item.supplier_name,
        invoice_date=item.invoice_date,
        currency=item.currency,
        total_amount=_canonical_decimal(item.total_amount),
        workflow=item.workflow.value,
        status=item.status.value,
        review_reasons=[_serialize_reason(reason) for reason in item.review_reasons],
        warnings=[str(warning) for warning in item.warnings],
        version=1,
        idempotency_key=idempotency_key,
    )


def _evidence_model_from_review_evidence(evidence: ReviewExecutionEvidence) -> WorkbenchReviewExecutionEvidence:
    payload = _execution_source_payload_from_review_evidence(evidence)
    return WorkbenchReviewExecutionEvidence(
        review_id=evidence.review_id,
        company_id=evidence.company_id,
        review_version=evidence.review_version,
        source_invoice_id=evidence.source_invoice_id,
        schema_version=REVIEW_EVIDENCE_SCHEMA_VERSION,
        invoice=payload["invoice"],
        partner_match=payload["partner_match"],
        product_match=payload["product_match"],
        tax_match=payload["tax_match"],
    )


def _review_evidence_from_model(record: WorkbenchReviewExecutionEvidence) -> ReviewExecutionEvidence:
    if record.schema_version != REVIEW_EVIDENCE_SCHEMA_VERSION:
        raise ReviewDataIntegrityError("Review execution evidence schema version is not supported.")
    source = deserialize_execution_source_invoice_payload(
        {
            "review_id": record.review_id,
            "company_id": record.company_id,
            "decision_version": record.review_version,
            "source_invoice_id": record.source_invoice_id,
            "invoice": record.invoice,
            "partner_match": record.partner_match,
            "product_match": record.product_match,
            "tax_match": record.tax_match,
        }
    )
    return _review_evidence_from_execution_source(source)


def _billing_evidence_model(evidence: ReviewExecutionBillingEvidence) -> WorkbenchReviewBillingEvidence:
    instruction = evidence.billing_instruction
    return WorkbenchReviewBillingEvidence(
        review_id=evidence.review_id,
        company_id=evidence.company_id,
        review_version=evidence.review_version,
        billing_key=instruction.billing_key,
        schema_version=REVIEW_BILLING_EVIDENCE_SCHEMA_VERSION,
        billing_instruction=serialize_billing_instruction_payload(instruction),
    )


def _billing_evidence_from_model(record: WorkbenchReviewBillingEvidence) -> ReviewExecutionBillingEvidence:
    if record.schema_version != REVIEW_BILLING_EVIDENCE_SCHEMA_VERSION:
        raise ReviewDataIntegrityError("Review billing evidence schema version is not supported.")
    instruction = deserialize_billing_instruction_payload(record.billing_instruction)
    if instruction.billing_key != record.billing_key:
        raise ReviewDataIntegrityError("Review billing evidence is invalid.")
    return ReviewExecutionBillingEvidence(
        review_id=record.review_id,
        company_id=record.company_id,
        review_version=record.review_version,
        billing_instruction=instruction,
    )


def _execution_source_payload_from_review_evidence(evidence: ReviewExecutionEvidence) -> dict[str, Any]:
    return serialize_execution_source_invoice_payload(_execution_source_from_review_evidence(evidence))


def _execution_source_from_review_evidence(evidence: ReviewExecutionEvidence) -> ExecutionSourceInvoice:
    return ExecutionSourceInvoice(
        review_id=evidence.review_id,
        company_id=evidence.company_id,
        decision_version=evidence.review_version,
        source_invoice_id=evidence.source_invoice_id,
        invoice=evidence.invoice,
        partner_match=evidence.partner_match,
        product_match=evidence.product_match,
        tax_match=evidence.tax_match,
    )


def _review_evidence_from_execution_source(source: ExecutionSourceInvoice) -> ReviewExecutionEvidence:
    return ReviewExecutionEvidence(
        review_id=source.review_id,
        company_id=source.company_id,
        review_version=source.decision_version,
        source_invoice_id=source.source_invoice_id,
        invoice=source.invoice,
        partner_match=source.partner_match,
        product_match=source.product_match,
        tax_match=source.tax_match,
    )


def _decision_model_from_command(
    command: ReviewDecisionCommand,
    *,
    decision_id: str,
) -> WorkbenchReviewDecision:
    return WorkbenchReviewDecision(
        decision_id=decision_id,
        review_id=command.review_id,
        company_id=command.company_id,
        review_version_before=command.expected_version,
        review_version_after=command.expected_version + 1,
        decision_type=command.decision.value,
        selected_workflow=command.selected_workflow.value if command.selected_workflow is not None else None,
        selected_partner_id=command.selected_partner_id,
        line_resolutions=[_serialize_line_resolution(resolution) for resolution in command.line_resolutions],
        tax_resolutions=[_serialize_tax_resolution(resolution) for resolution in command.tax_resolutions],
        business_context=None,
        business_context_allocations=_serialize_business_context_allocations(command.business_context_allocations),
        comment=command.comment,
        decided_by=command.decided_by,
        idempotency_key=command.idempotency_key,
    )


def _acknowledgement_from_decision_model(record: WorkbenchReviewDecision) -> ReviewDecisionAcknowledgement:
    try:
        decision = ReviewDecisionType(record.decision_type)
        selected_workflow = WorkflowType(record.selected_workflow) if record.selected_workflow is not None else None
        return ReviewDecisionAcknowledgement(
            accepted=True,
            review_id=record.review_id,
            status=_target_status_for_decision(decision),
            version=record.review_version_after,
            decision=decision,
            selected_workflow=selected_workflow,
        )
    except ReviewDecisionDataIntegrityError:
        raise
    except (TypeError, ValueError) as exc:
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.") from exc


def _accepted_review_decision_from_model(record: WorkbenchReviewDecision) -> AcceptedReviewDecision:
    try:
        return AcceptedReviewDecision(
            review_id=record.review_id,
            company_id=record.company_id,
            decision_version=record.review_version_after,
            decision_id=record.decision_id,
            selected_workflow=WorkflowType(record.selected_workflow) if record.selected_workflow is not None else None,
            business_context_allocations=_deserialize_business_context_allocations(record.business_context_allocations),
            decision_type=ReviewDecisionType(record.decision_type),
        )
    except ReviewDecisionDataIntegrityError:
        raise
    except (TypeError, ValueError) as exc:
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.") from exc


def _review_item_from_model(record: WorkbenchReviewItem) -> ReviewItem:
    try:
        return ReviewItem(
            review_id=record.review_id,
            invoice_id=record.invoice_id,
            invoice_number=record.invoice_number,
            supplier_tax_number=record.supplier_tax_number,
            supplier_name=record.supplier_name,
            invoice_date=record.invoice_date,
            currency=record.currency,
            total_amount=record.total_amount,
            workflow=WorkflowType(record.workflow),
            status=ReviewStatus(record.status),
            review_reasons=tuple(_deserialize_reason(reason) for reason in _require_list(record.review_reasons)),
            warnings=tuple(_deserialize_warnings(record.warnings)),
            created_at=record.created_at,
            updated_at=record.updated_at,
            version=record.version,
        )
    except ReviewDataIntegrityError:
        raise
    except (TypeError, ValueError) as exc:
        raise ReviewDataIntegrityError("Persisted review item data is invalid.") from exc


def _serialize_reason(reason: ManualReviewReason) -> dict[str, Any]:
    return {
        "code": reason.code.value,
        "message": reason.message,
        "line_number": reason.line_number,
        "tax_index": reason.tax_index,
        "candidate_count": reason.candidate_count,
        "source": reason.source,
        "details": [[str(key), str(value)] for key, value in reason.details],
    }


def _serialize_line_resolution(resolution: LineResolution) -> dict[str, Any]:
    return {
        "line_number": resolution.line_number,
        "selected_product_id": resolution.selected_product_id,
    }


def _deserialize_line_resolution(value: Any) -> LineResolution:
    if not isinstance(value, dict):
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.")
    return LineResolution(
        line_number=str(value["line_number"]),
        selected_product_id=_required_int(value.get("selected_product_id")),
    )


def _serialize_tax_resolution(resolution: TaxResolution) -> dict[str, Any]:
    return {
        "line_number": resolution.line_number,
        "tax_index": resolution.tax_index,
        "selected_tax_id": resolution.selected_tax_id,
    }


def _deserialize_tax_resolution(value: Any) -> TaxResolution:
    if not isinstance(value, dict):
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.")
    return TaxResolution(
        line_number=str(value["line_number"]),
        tax_index=_required_int(value.get("tax_index")),
        selected_tax_id=_required_int(value.get("selected_tax_id")),
    )


def _serialize_business_context(context: BusinessContextDecision | None) -> dict[str, int] | None:
    if context is None:
        return None
    return {
        key: value
        for key, value in {
            "opportunity_id": context.opportunity_id,
            "sales_order_id": context.sales_order_id,
            "proposal_scenario_id": context.proposal_scenario_id,
            "purchase_order_id": context.purchase_order_id,
            "project_id": context.project_id,
            "analytic_account_id": context.analytic_account_id,
        }.items()
        if value is not None
    }


def _deserialize_business_context(value: Any) -> BusinessContextDecision | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.")
    return BusinessContextDecision(
        opportunity_id=_optional_decision_int(value.get("opportunity_id")),
        sales_order_id=_optional_decision_int(value.get("sales_order_id")),
        proposal_scenario_id=_optional_decision_int(value.get("proposal_scenario_id")),
        purchase_order_id=_optional_decision_int(value.get("purchase_order_id")),
        project_id=_optional_decision_int(value.get("project_id")),
        analytic_account_id=_optional_decision_int(value.get("analytic_account_id")),
    )


def _serialize_business_context_allocations(context: BusinessContextAllocationSet | None) -> dict[str, Any] | None:
    if context is None:
        return None
    payload: dict[str, Any] = {
        "completeness": context.completeness.value,
        "allocations": [
            _serialize_business_context_allocation(allocation)
            for allocation in sorted(context.allocations, key=lambda allocation: allocation.allocation_key)
        ],
    }
    if context.invoice_total is not None:
        payload["invoice_total"] = _canonical_decimal_text(context.invoice_total)
    if context.currency is not None:
        payload["currency"] = context.currency
    return payload


def _serialize_business_context_allocation(allocation: BusinessContextAllocation) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "allocation_key": allocation.allocation_key,
        "allocation_type": allocation.allocation_type.value,
    }
    optional_values: dict[str, Any] = {
        "source_line_number": allocation.source_line_number,
        "description": allocation.description,
        "amount": _canonical_decimal_text(allocation.amount),
        "percentage": _canonical_decimal_text(allocation.percentage),
        "currency": allocation.currency,
        "customer_id": allocation.customer_id,
        "recharge_partner_id": allocation.recharge_partner_id,
        "customer_invoice_id": allocation.customer_invoice_id,
        "target_company_id": allocation.target_company_id,
        "opportunity_id": allocation.opportunity_id,
        "sales_order_id": allocation.sales_order_id,
        "sales_order_line_id": allocation.sales_order_line_id,
        "proposal_scenario_id": allocation.proposal_scenario_id,
        "purchase_order_id": allocation.purchase_order_id,
        "project_id": allocation.project_id,
        "analytic_account_id": allocation.analytic_account_id,
        "subscription_id": allocation.subscription_id,
        "internal_note": allocation.internal_note,
    }
    payload.update({key: value for key, value in optional_values.items() if value is not None})
    return payload


def _deserialize_business_context_allocations(value: Any) -> BusinessContextAllocationSet | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.")
    try:
        allocations = value["allocations"]
        if not isinstance(allocations, list):
            raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.")
        return BusinessContextAllocationSet(
            allocations=tuple(_deserialize_business_context_allocation(allocation) for allocation in allocations),
            completeness=AllocationCompleteness(str(value.get("completeness", AllocationCompleteness.COMPLETE.value))),
            invoice_total=_optional_decimal_text(value.get("invoice_total")),
            currency=_optional_decision_text(value.get("currency")),
        )
    except ReviewDecisionDataIntegrityError:
        raise
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.") from exc


def _deserialize_business_context_allocation(value: Any) -> BusinessContextAllocation:
    if not isinstance(value, dict):
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.")
    return BusinessContextAllocation(
        allocation_key=_required_text(value.get("allocation_key")),
        allocation_type=BusinessContextAllocationType(str(value.get("allocation_type"))),
        source_line_number=_optional_decision_text(value.get("source_line_number")),
        description=_optional_decision_text(value.get("description")),
        amount=_optional_decimal_text(value.get("amount")),
        percentage=_optional_decimal_text(value.get("percentage")),
        currency=_optional_decision_text(value.get("currency")),
        customer_id=_optional_decision_int(value.get("customer_id")),
        recharge_partner_id=_optional_decision_int(value.get("recharge_partner_id")),
        customer_invoice_id=_optional_decision_int(value.get("customer_invoice_id")),
        target_company_id=_optional_decision_int(value.get("target_company_id")),
        opportunity_id=_optional_decision_int(value.get("opportunity_id")),
        sales_order_id=_optional_decision_int(value.get("sales_order_id")),
        sales_order_line_id=_optional_decision_int(value.get("sales_order_line_id")),
        proposal_scenario_id=_optional_decision_int(value.get("proposal_scenario_id")),
        purchase_order_id=_optional_decision_int(value.get("purchase_order_id")),
        project_id=_optional_decision_int(value.get("project_id")),
        analytic_account_id=_optional_decision_int(value.get("analytic_account_id")),
        subscription_id=_optional_decision_int(value.get("subscription_id")),
        internal_note=_optional_decision_text(value.get("internal_note")),
    )


def _deserialize_reason(value: Any) -> ManualReviewReason:
    if not isinstance(value, dict):
        raise ReviewDataIntegrityError("Persisted review item data is invalid.")
    details = value.get("details", [])
    if not isinstance(details, list):
        raise ReviewDataIntegrityError("Persisted review item data is invalid.")
    return ManualReviewReason(
        code=ManualReviewReasonCode(str(value["code"])),
        message=str(value["message"]),
        line_number=_optional_text(value.get("line_number")),
        tax_index=_optional_int(value.get("tax_index")),
        candidate_count=_optional_int(value.get("candidate_count")),
        source=_optional_text(value.get("source")),
        details=tuple(_detail_pair(pair) for pair in details),
    )


def _deserialize_warnings(value: Any) -> Iterable[str]:
    for warning in _require_list(value):
        if not isinstance(warning, str):
            raise ReviewDataIntegrityError("Persisted review item data is invalid.")
        yield warning


def _require_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ReviewDataIntegrityError("Persisted review item data is invalid.")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReviewDataIntegrityError("Persisted review item data is invalid.")
    return value


def _required_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.")
    return value


def _optional_decision_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.")
    return value


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ReviewDataIntegrityError("Persisted review item data is invalid.")
    return value


def _optional_decision_int(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.")
    return value


def _optional_decimal_text(value: Any) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.")
    try:
        decimal = Decimal(value)
    except InvalidOperation as exc:
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.") from exc
    if not decimal.is_finite():
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.")
    return decimal


def _required_int(value: Any) -> int:
    if type(value) is not int:
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.")
    return value


def _detail_pair(value: Any) -> tuple[str, str]:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise ReviewDataIntegrityError("Persisted review item data is invalid.")
    key, detail_value = value
    if not isinstance(key, str) or not isinstance(detail_value, str):
        raise ReviewDataIntegrityError("Persisted review item data is invalid.")
    return key, detail_value


def _business_fingerprint(item: ReviewItem, *, company_id: int) -> tuple[Any, ...]:
    return (
        company_id,
        item.review_id,
        item.invoice_id,
        item.invoice_number,
        item.supplier_tax_number,
        item.supplier_name,
        item.invoice_date,
        item.currency,
        _canonical_decimal(item.total_amount),
        item.workflow,
        item.status,
        tuple(_reason_fingerprint(reason) for reason in item.review_reasons),
        item.warnings,
        item.version,
    )


def _review_evidence_fingerprint(evidence: ReviewExecutionEvidence) -> tuple[Any, ...]:
    payload = _execution_source_payload_from_review_evidence(evidence)
    return (
        payload["review_id"],
        payload["company_id"],
        payload["decision_version"],
        payload["source_invoice_id"],
        payload["invoice"],
        payload["partner_match"],
        payload["product_match"],
        payload["tax_match"],
    )


def _billing_evidence_fingerprint(evidence: tuple[ReviewExecutionBillingEvidence, ...]) -> tuple[Any, ...]:
    payloads = tuple(
        (
            item.review_id,
            item.company_id,
            item.review_version,
            serialize_billing_instruction_payload(item.billing_instruction),
        )
        for item in evidence
    )
    return tuple(sorted(payloads, key=lambda payload: str(payload[3]["billing_key"])))


def _billing_evidence_identity(evidence: tuple[ReviewExecutionBillingEvidence, ...]) -> tuple[str, int, int]:
    if not evidence:
        raise WorkbenchContractError("Review billing evidence is required.")
    first = evidence[0]
    identity = (first.review_id, first.company_id, first.review_version)
    if any((item.review_id, item.company_id, item.review_version) != identity for item in evidence):
        raise WorkbenchContractError("Review billing evidence identity must be consistent.")
    return identity


def _decision_fingerprint(command: ReviewDecisionCommand) -> tuple[Any, ...]:
    return (
        command.company_id,
        command.review_id,
        command.expected_version,
        command.decision,
        command.selected_workflow,
        command.selected_partner_id,
        tuple(_line_resolution_fingerprint(resolution) for resolution in command.line_resolutions),
        tuple(_tax_resolution_fingerprint(resolution) for resolution in command.tax_resolutions),
        _business_context_allocations_fingerprint(command.business_context_allocations),
        command.comment,
        command.decided_by,
    )


def _decision_fingerprint_from_model(record: WorkbenchReviewDecision) -> tuple[Any, ...]:
    try:
        command_like = ReviewDecisionCommand(
            review_id=record.review_id,
            company_id=record.company_id,
            expected_version=record.review_version_before,
            decision=ReviewDecisionType(record.decision_type),
            selected_workflow=WorkflowType(record.selected_workflow) if record.selected_workflow is not None else None,
            selected_partner_id=record.selected_partner_id,
            line_resolutions=tuple(
                _deserialize_line_resolution(resolution) for resolution in _require_list(record.line_resolutions)
            ),
            tax_resolutions=tuple(
                _deserialize_tax_resolution(resolution) for resolution in _require_list(record.tax_resolutions)
            ),
            business_context_allocations=_deserialize_business_context_allocations(record.business_context_allocations),
            comment=record.comment,
            decided_by=record.decided_by,
            idempotency_key=record.idempotency_key,
        )
    except ReviewDecisionDataIntegrityError:
        raise
    except (TypeError, ValueError) as exc:
        raise ReviewDecisionDataIntegrityError("Persisted review decision data is invalid.") from exc
    if record.business_context is not None:
        legacy_context = _deserialize_business_context(record.business_context)
        if command_like.business_context_allocations is None:
            return (
                *_decision_fingerprint(command_like),
                ("legacy_business_context", _business_context_fingerprint(legacy_context)),
            )
    return _decision_fingerprint(command_like)


def _line_resolution_fingerprint(resolution: LineResolution) -> tuple[str, int]:
    return resolution.line_number, resolution.selected_product_id


def _tax_resolution_fingerprint(resolution: TaxResolution) -> tuple[str, int, int]:
    return resolution.line_number, resolution.tax_index, resolution.selected_tax_id


def _business_context_fingerprint(context: BusinessContextDecision | None) -> tuple[tuple[str, int], ...] | None:
    serialized = _serialize_business_context(context)
    if serialized is None:
        return None
    return tuple((key, serialized[key]) for key in sorted(serialized))


def _business_context_allocations_fingerprint(context: BusinessContextAllocationSet | None) -> tuple[Any, ...] | None:
    serialized = _serialize_business_context_allocations(context)
    if serialized is None:
        return None
    return (
        serialized["completeness"],
        serialized.get("invoice_total"),
        serialized.get("currency"),
        tuple(tuple((key, allocation[key]) for key in sorted(allocation)) for allocation in serialized["allocations"]),
    )


def _evidence_fingerprint(
    evidence: ExecutionSourceInvoice,
    *,
    decision_id: str,
) -> tuple[Any, ...]:
    return _canonical_mapping_fingerprint(serialize_execution_source_invoice(evidence, decision_id=decision_id))


def _evidence_fingerprint_from_model(record: ExecutionSourceInvoiceEvidence) -> tuple[Any, ...]:
    return _canonical_mapping_fingerprint(
        {
            "schema_version": record.schema_version,
            "review_id": record.review_id,
            "company_id": record.company_id,
            "decision_version": record.decision_version,
            "decision_id": record.decision_id,
            "source_invoice_id": record.source_invoice_id,
            "invoice": record.invoice,
            "partner_match": record.partner_match,
            "product_match": record.product_match,
            "tax_match": record.tax_match,
        }
    )


def _canonical_mapping_fingerprint(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple((str(key), _canonical_mapping_fingerprint(value[key])) for key in sorted(value))
    if isinstance(value, list | tuple):
        return tuple(_canonical_mapping_fingerprint(item) for item in value)
    return value


def _target_status_for_decision(decision: ReviewDecisionType) -> ReviewStatus:
    if decision is ReviewDecisionType.SELECT_WORKFLOW:
        return ReviewStatus.DECISION_SUBMITTED
    if decision is ReviewDecisionType.DISMISS:
        return ReviewStatus.DISMISSED
    raise WorkbenchContractError("Unsupported review decision.")


def _reason_fingerprint(reason: ManualReviewReason) -> tuple[Any, ...]:
    serialized = _serialize_reason(reason)
    return (
        serialized["code"],
        serialized["message"],
        serialized["line_number"],
        serialized["tax_index"],
        serialized["candidate_count"],
        serialized["source"],
        tuple(tuple(pair) for pair in serialized["details"]),
    )


def _canonical_decimal(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if not value.is_finite():
        raise WorkbenchContractError("total_amount must be a finite Decimal value.")
    try:
        normalized = value.normalize()
    except InvalidOperation as exc:
        raise WorkbenchContractError("total_amount must be a valid Decimal value.") from exc
    if normalized == Decimal("0"):
        return Decimal("0")
    return normalized


def _canonical_decimal_text(value: Decimal | None) -> str | None:
    canonical = _canonical_decimal(value)
    if canonical is None:
        return None
    return format(canonical, "f")


def _validate_supported_amount(value: Decimal | None) -> None:
    canonical = _canonical_decimal(value)
    if canonical is None:
        return
    if _decimal_scale(canonical) > REVIEW_AMOUNT_SCALE:
        raise WorkbenchContractError(f"total_amount supports at most {REVIEW_AMOUNT_SCALE} fractional digits.")
    if _decimal_integer_digits(canonical) > REVIEW_AMOUNT_INTEGER_DIGITS:
        raise WorkbenchContractError(f"total_amount supports at most {REVIEW_AMOUNT_PRECISION} total digits.")


def _decimal_scale(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return abs(exponent) if exponent < 0 else 0


def _decimal_integer_digits(value: Decimal) -> int:
    adjusted = abs(value).adjusted()
    return adjusted + 1 if adjusted >= 0 else 0
