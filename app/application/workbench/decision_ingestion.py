from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.application.dto import ApplicationDTO
from app.application.workbench.allocations import BusinessContextAllocation, BusinessContextAllocationSet
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.decision_use_cases import SubmitReviewDecisionUseCase
from app.application.workbench.exceptions import (
    ReviewDecisionError,
    ReviewDecisionIdempotencyConflictError,
    ReviewNotFoundError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
    WorkbenchCandidateAmbiguityError,
    WorkbenchCandidateDataError,
    WorkbenchCandidateReadError,
    WorkbenchCandidateUnsupportedDecisionError,
    WorkbenchContractError,
    WorkbenchErpReferenceValidationError,
    WorkbenchProjectionPublishError,
    WorkbenchSubmissionCompanyMismatchError,
)
from app.application.workbench.ports import (
    WorkbenchDecisionCandidateReader,
    WorkbenchProjectionPublisher,
)
from app.application.workbench.projection import (
    OdooWorkbenchDecisionCandidate,
    OdooWorkbenchDecisionCandidateReadFailure,
)

DEFAULT_DECISION_INGESTION_LIMIT = 50


class WorkbenchDecisionIngestionStatus(StrEnum):
    """Safe result states for one ready Odoo Workbench decision candidate."""

    PROCESSED = "processed"
    ALREADY_PROCESSED = "already_processed"
    UNSUPPORTED_DECISION = "unsupported_decision"
    REVIEW_NOT_FOUND = "review_not_found"
    COMPANY_MISMATCH = "company_mismatch"
    STALE_REVIEW_VERSION = "stale_review_version"
    INVALID_WORKFLOW = "invalid_workflow"
    INVALID_ALLOCATION = "invalid_allocation"
    ACKNOWLEDGEMENT_FAILED = "acknowledgement_failed"
    READ_FAILED = "read_failed"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class WorkbenchDecisionIngestionCandidateResult(ApplicationDTO):
    """Safe immutable result for one candidate ingestion attempt."""

    review_id: str | None
    odoo_record_id: int | None
    status: WorkbenchDecisionIngestionStatus
    acknowledged: bool = False
    idempotency_key: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, WorkbenchDecisionIngestionStatus):
            raise WorkbenchContractError("status must be a canonical decision ingestion status.")
        if type(self.acknowledged) is not bool:
            raise WorkbenchContractError("acknowledged must be a boolean value.")
        if self.review_id is not None and not self.review_id.strip():
            raise WorkbenchContractError("review_id must be non-empty when supplied.")
        if self.odoo_record_id is not None and (type(self.odoo_record_id) is not int or self.odoo_record_id <= 0):
            raise WorkbenchContractError("odoo_record_id must be a positive ERP id when supplied.")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise WorkbenchContractError("idempotency_key must be non-empty when supplied.")


@dataclass(frozen=True, slots=True)
class WorkbenchDecisionIngestionResult(ApplicationDTO):
    """Safe immutable result for one explicit Odoo Workbench ingestion run."""

    company_id: int
    processed_count: int
    already_processed_count: int
    acknowledgement_failed_count: int
    failed_count: int
    results: tuple[WorkbenchDecisionIngestionCandidateResult, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if type(self.company_id) is not int or self.company_id <= 0:
            raise WorkbenchContractError("company_id must be positive.")
        results = tuple(self.results)
        object.__setattr__(self, "results", results)
        _require_nonnegative(self.processed_count, "processed_count")
        _require_nonnegative(self.already_processed_count, "already_processed_count")
        _require_nonnegative(self.acknowledgement_failed_count, "acknowledgement_failed_count")
        _require_nonnegative(self.failed_count, "failed_count")


class UnitOfWork(Protocol):
    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class WorkbenchErpReferenceValidator(Protocol):
    def validate(self, candidate: OdooWorkbenchDecisionCandidate, *, requested_company_id: int) -> object:
        pass


class WorkbenchDecisionIngestionWorkflow:
    """Read ready Odoo decisions, persist canonical Hub evidence, then acknowledge Odoo."""

    def __init__(
        self,
        *,
        candidate_reader: WorkbenchDecisionCandidateReader,
        erp_reference_validator: WorkbenchErpReferenceValidator,
        decision_submitter: SubmitReviewDecisionUseCase,
        acknowledgement_publisher: WorkbenchProjectionPublisher,
        unit_of_work: UnitOfWork,
        idempotency_key_factory: Callable[[OdooWorkbenchDecisionCandidate], str] | None = None,
    ) -> None:
        self._candidate_reader = candidate_reader
        self._erp_reference_validator = erp_reference_validator
        self._decision_submitter = decision_submitter
        self._acknowledgement_publisher = acknowledgement_publisher
        self._unit_of_work = unit_of_work
        self._idempotency_key_factory = idempotency_key_factory or decision_idempotency_key

    def sync_ready_decisions(
        self,
        *,
        company_id: int,
        limit: int = DEFAULT_DECISION_INGESTION_LIMIT,
        trace_id: str | None = None,
    ) -> WorkbenchDecisionIngestionResult:
        if type(company_id) is not int or company_id <= 0:
            raise WorkbenchContractError("company_id must be positive.")
        if type(limit) is not int or limit <= 0:
            raise WorkbenchContractError("limit must be positive.")
        try:
            candidates = self._list_ready_decision_results(company_id=company_id, limit=limit)
        except WorkbenchCandidateReadError as exc:
            return _result(company_id, (_failure(None, None, WorkbenchDecisionIngestionStatus.READ_FAILED, exc),))

        results = tuple(
            self._ingest_candidate_result(candidate, company_id=company_id, trace_id=trace_id)
            for candidate in candidates
        )
        return _result(company_id, results)

    def _list_ready_decision_results(
        self,
        *,
        company_id: int,
        limit: int,
    ) -> tuple[OdooWorkbenchDecisionCandidate | OdooWorkbenchDecisionCandidateReadFailure, ...]:
        scanner = getattr(self._candidate_reader, "list_ready_decision_results", None)
        if scanner is not None:
            return tuple(scanner(company_id=company_id, limit=limit))
        return self._candidate_reader.list_ready_decisions(company_id=company_id, limit=limit)

    def _ingest_candidate_result(
        self,
        candidate: OdooWorkbenchDecisionCandidate | OdooWorkbenchDecisionCandidateReadFailure,
        *,
        company_id: int,
        trace_id: str | None,
    ) -> WorkbenchDecisionIngestionCandidateResult:
        if isinstance(candidate, OdooWorkbenchDecisionCandidateReadFailure):
            return _expected_failure(candidate.review_id, candidate.odoo_record_id, candidate.error)
        return self._ingest_candidate(candidate, company_id=company_id, trace_id=trace_id)

    def _ingest_candidate(
        self,
        candidate: OdooWorkbenchDecisionCandidate,
        *,
        company_id: int,
        trace_id: str | None,
    ) -> WorkbenchDecisionIngestionCandidateResult:
        review_id = candidate.review_id
        odoo_record_id = candidate.odoo_record_id
        try:
            if candidate.company_id != company_id:
                return _failure(
                    review_id,
                    odoo_record_id,
                    WorkbenchDecisionIngestionStatus.COMPANY_MISMATCH,
                    WorkbenchSubmissionCompanyMismatchError(
                        "Odoo Workbench decision candidate company scope mismatch."
                    ),
                )
            self._erp_reference_validator.validate(candidate, requested_company_id=company_id)
            command = _review_decision_command(candidate, idempotency_key=self._idempotency_key_factory(candidate))
            already_processed = self._is_matching_decision_already_persisted(command)
            acknowledgement = self._decision_submitter.execute(command)
            self._unit_of_work.commit()
            try:
                self._acknowledgement_publisher.acknowledge_decision(
                    acknowledgement,
                    odoo_record_id=odoo_record_id,
                    trace_id=trace_id,
                    idempotency_key=command.idempotency_key,
                    clear_ready=True,
                )
            except WorkbenchProjectionPublishError as exc:
                return _candidate_result(
                    review_id=review_id,
                    odoo_record_id=odoo_record_id,
                    status=WorkbenchDecisionIngestionStatus.ACKNOWLEDGEMENT_FAILED,
                    acknowledged=False,
                    idempotency_key=command.idempotency_key,
                    message=str(exc),
                )
            return _candidate_result(
                review_id=review_id,
                odoo_record_id=odoo_record_id,
                status=_accepted_status(already_processed=already_processed),
                acknowledged=True,
                idempotency_key=command.idempotency_key,
            )
        except (ReviewNotFoundError, ReviewVersionConflictError, ReviewStateConflictError) as exc:
            self._unit_of_work.rollback()
            return _expected_failure(review_id, odoo_record_id, exc)
        except (
            WorkbenchCandidateAmbiguityError,
            WorkbenchCandidateDataError,
            WorkbenchContractError,
            WorkbenchErpReferenceValidationError,
            ReviewDecisionIdempotencyConflictError,
        ) as exc:
            self._unit_of_work.rollback()
            return _expected_failure(review_id, odoo_record_id, exc)
        except ReviewDecisionError as exc:
            self._unit_of_work.rollback()
            return _failure(review_id, odoo_record_id, WorkbenchDecisionIngestionStatus.CONFLICT, exc)

    def _is_matching_decision_already_persisted(self, command: ReviewDecisionCommand) -> bool:
        checker = getattr(self._decision_submitter, "has_matching_decision", None)
        if checker is None:
            return False
        return bool(checker(command))


def _review_decision_command(
    candidate: OdooWorkbenchDecisionCandidate,
    *,
    idempotency_key: str,
) -> ReviewDecisionCommand:
    return ReviewDecisionCommand(
        review_id=candidate.review_id,
        company_id=candidate.company_id,
        expected_version=candidate.expected_version,
        decision=candidate.decision,
        decided_by=f"odoo:{candidate.decided_by_odoo_user_id}",
        idempotency_key=idempotency_key,
        selected_workflow=candidate.selected_workflow,
        selected_partner_id=candidate.selected_partner_id,
        line_resolutions=candidate.line_resolutions,
        tax_resolutions=candidate.tax_resolutions,
        business_context_allocations=candidate.business_context_allocations,
        comment=candidate.comment,
    )


def decision_idempotency_key(candidate: OdooWorkbenchDecisionCandidate) -> str:
    payload = {
        "company_id": candidate.company_id,
        "review_id": candidate.review_id,
        "expected_version": candidate.expected_version,
        "decision": candidate.decision.value,
        "selected_workflow": candidate.selected_workflow.value if candidate.selected_workflow is not None else None,
        "selected_partner_id": candidate.selected_partner_id,
        "line_resolutions": [
            {
                "line_number": resolution.line_number,
                "selected_product_id": resolution.selected_product_id,
            }
            for resolution in candidate.line_resolutions
        ],
        "tax_resolutions": [
            {
                "line_number": resolution.line_number,
                "tax_index": resolution.tax_index,
                "selected_tax_id": resolution.selected_tax_id,
            }
            for resolution in candidate.tax_resolutions
        ],
        "business_context_allocations": _allocation_set_payload(candidate.business_context_allocations),
        "comment": candidate.comment,
        "decided_by": f"odoo:{candidate.decided_by_odoo_user_id}",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"odoo-workbench-decision:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _allocation_set_payload(allocations: BusinessContextAllocationSet | None) -> dict[str, object] | None:
    if allocations is None:
        return None
    return {
        "completeness": allocations.completeness.value,
        "invoice_total": _decimal_text(allocations.invoice_total),
        "currency": allocations.currency,
        "allocations": [_allocation_payload(allocation) for allocation in allocations.allocations],
    }


def _allocation_payload(allocation: BusinessContextAllocation) -> dict[str, object]:
    return {
        "allocation_key": allocation.allocation_key,
        "allocation_type": allocation.allocation_type.value,
        "source_line_number": allocation.source_line_number,
        "description": allocation.description,
        "amount": _decimal_text(allocation.amount),
        "percentage": _decimal_text(allocation.percentage),
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


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.normalize())


def _accepted_status(*, already_processed: bool) -> WorkbenchDecisionIngestionStatus:
    if already_processed:
        return WorkbenchDecisionIngestionStatus.ALREADY_PROCESSED
    return WorkbenchDecisionIngestionStatus.PROCESSED


def _expected_failure(
    review_id: str | None,
    odoo_record_id: int | None,
    exc: Exception,
) -> WorkbenchDecisionIngestionCandidateResult:
    if isinstance(exc, WorkbenchCandidateUnsupportedDecisionError):
        return _failure(review_id, odoo_record_id, WorkbenchDecisionIngestionStatus.UNSUPPORTED_DECISION, exc)
    if isinstance(exc, ReviewNotFoundError):
        return _failure(review_id, odoo_record_id, WorkbenchDecisionIngestionStatus.REVIEW_NOT_FOUND, exc)
    if isinstance(exc, ReviewVersionConflictError | ReviewStateConflictError):
        return _failure(review_id, odoo_record_id, WorkbenchDecisionIngestionStatus.STALE_REVIEW_VERSION, exc)
    if isinstance(exc, WorkbenchSubmissionCompanyMismatchError):
        return _failure(review_id, odoo_record_id, WorkbenchDecisionIngestionStatus.COMPANY_MISMATCH, exc)
    if isinstance(exc, ReviewDecisionIdempotencyConflictError):
        return _failure(review_id, odoo_record_id, WorkbenchDecisionIngestionStatus.CONFLICT, exc)
    if isinstance(exc, WorkbenchCandidateDataError | WorkbenchErpReferenceValidationError | WorkbenchContractError):
        return _failure(review_id, odoo_record_id, WorkbenchDecisionIngestionStatus.INVALID_ALLOCATION, exc)
    return _failure(review_id, odoo_record_id, WorkbenchDecisionIngestionStatus.CONFLICT, exc)


def _failure(
    review_id: str | None,
    odoo_record_id: int | None,
    status: WorkbenchDecisionIngestionStatus,
    exc: Exception,
) -> WorkbenchDecisionIngestionCandidateResult:
    return _candidate_result(
        review_id=review_id,
        odoo_record_id=odoo_record_id,
        status=status,
        message=str(getattr(exc, "safe_message", str(exc))),
    )


def _candidate_result(
    *,
    review_id: str | None,
    odoo_record_id: int | None,
    status: WorkbenchDecisionIngestionStatus,
    acknowledged: bool = False,
    idempotency_key: str | None = None,
    message: str | None = None,
) -> WorkbenchDecisionIngestionCandidateResult:
    return WorkbenchDecisionIngestionCandidateResult(
        review_id=review_id,
        odoo_record_id=odoo_record_id,
        status=status,
        acknowledged=acknowledged,
        idempotency_key=idempotency_key,
        message=message,
    )


def _result(
    company_id: int,
    results: tuple[WorkbenchDecisionIngestionCandidateResult, ...],
) -> WorkbenchDecisionIngestionResult:
    return WorkbenchDecisionIngestionResult(
        company_id=company_id,
        processed_count=sum(1 for result in results if result.status is WorkbenchDecisionIngestionStatus.PROCESSED),
        already_processed_count=sum(
            1 for result in results if result.status is WorkbenchDecisionIngestionStatus.ALREADY_PROCESSED
        ),
        acknowledgement_failed_count=sum(
            1 for result in results if result.status is WorkbenchDecisionIngestionStatus.ACKNOWLEDGEMENT_FAILED
        ),
        failed_count=sum(
            1
            for result in results
            if result.status
            not in {
                WorkbenchDecisionIngestionStatus.PROCESSED,
                WorkbenchDecisionIngestionStatus.ALREADY_PROCESSED,
            }
        ),
        results=results,
    )


def _require_nonnegative(value: int, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise WorkbenchContractError(f"{field_name} must be zero or greater.")
