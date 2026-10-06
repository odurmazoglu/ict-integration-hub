"""Canonical Hub -> Odoo Workbench projection synchronization (OPS-UI-01A).

The Hub is the source of truth; the Odoo Workbench row is a read-only projection
of it. Every projection-relevant Hub transition calls exactly one entry point,
:meth:`WorkbenchProjectionSynchronizer.sync`, *after* its own Hub commit:

    Hub business operation -> Hub commit succeeds -> projection sync attempt
        -> projection success OR logged, visible warning

``sync`` re-reads the *committed* Hub state and projects one complete snapshot
(review row, accepted decision, PR #197 effective line resolution, latest stored
EXECUTE-mode execution and its Vendor Bill artifact). No use case builds a partial
payload. Because every sync writes the full snapshot, a missed or failed sync is
repaired by syncing again; ``python -m app.cli.reconcile_workbench_projection`` is
the convergence/recovery path.

A projection failure never propagates into the business operation: ``sync``
returns an ``ERROR`` result (and logs it) instead of raising, so it can never roll
back Hub state, change an accepted decision or execution, or make a caller retry a
committed transition.

Read isolation: the synchronizer never uses the caller's persistence context. Each
``sync``/``plan``/``build_projection`` call opens its own read scope (a fresh,
read-only persistence context supplied by the composition root), reads the
committed snapshot through it, and closes it -- all in the calling thread. A call
made through ``asyncio.to_thread`` therefore creates, uses and closes its read
scope entirely inside the worker thread, and a failure can only ever discard that
private read scope, never the business transaction.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from app.application.exceptions import ApplicationError
from app.application.execution.contracts import (
    AcceptedReviewDecision,
    ExecutionArtifactType,
    ExecutionSourceInvoice,
)
from app.application.execution.exceptions import (
    ExecutionSourceInvoiceNotFoundError,
)
from app.application.execution.runtime import ExecutionSnapshot
from app.application.workbench.dto import ReviewItem, ReviewStatus, review_reasons_role
from app.application.workbench.exceptions import (
    ReviewNotFoundError,
    WorkbenchContractError,
)
from app.application.workbench.operator_guidance import (
    GuidanceInput,
    OperatorGuidanceFacts,
    WorkbenchOperatorGuidance,
    build_operator_guidance,
)
from app.application.workbench.projection import (
    WorkbenchProjection,
    WorkbenchProjectionDecision,
    WorkbenchProjectionExecution,
    WorkbenchProjectionLineResolution,
)
from app.application.workbench.projection_sync_contracts import (
    PROJECTION_SYNC_WARNING,
    ProjectionFieldChange,
    ProjectionSyncOutcome,
    ProjectionSyncResult,
    ReviewProjectionSynchronizer,
    WorkbenchProjectionSyncPublisher,
    projection_sync_warnings,
    sync_after_commit,
)
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.review_evidence import (
    ACCEPTED_EVIDENCE_INTEGRITY_ERRORS,
    EFFECTIVE_STATE_UNAVAILABLE,
    effective_resolutions,
)

logger = logging.getLogger(__name__)

SAFE_PROJECTION_SYNC_ERROR = "Odoo Workbench projection sync failed."


class _ReviewReader(Protocol):
    def get_review_item(self, query: ReviewDetailQuery) -> ReviewItem:
        pass


class _AcceptedDecisionReader(Protocol):
    def get_accepted_decision(
        self, *, review_id: str, company_id: int, decision_version: int
    ) -> AcceptedReviewDecision:
        pass


class _AcceptedSourceReader(Protocol):
    def get_source_invoice(self, *, review_id: str, company_id: int, decision_version: int) -> ExecutionSourceInvoice:
        pass


class _ExecutionSnapshotReader(Protocol):
    def find_latest_snapshot_for_review(self, *, review_id: str, company_id: int) -> ExecutionSnapshot | None:
        pass


@dataclass(frozen=True, slots=True)
class WorkbenchProjectionSources:
    """Everything one sync reads through, bound to one private read scope."""

    review_reader: _ReviewReader
    accepted_decision_reader: _AcceptedDecisionReader
    accepted_source_reader: _AcceptedSourceReader
    execution_snapshot_reader: _ExecutionSnapshotReader
    publisher: WorkbenchProjectionSyncPublisher
    #: ADR-0013: committed facts for operator guidance (purpose, accounting resolution,
    #: eligible asset accounts). ``None`` keeps the pre-ADR-0013 projection unchanged.
    guidance_facts_reader: Callable[[ReviewItem, int], OperatorGuidanceFacts] | None = None


#: Opens one private, read-only scope over *committed* Hub state and closes it on exit.
ProjectionReadScope = Callable[[], AbstractContextManager[WorkbenchProjectionSources]]


class WorkbenchProjectionSynchronizer:
    """The single Hub -> Odoo Workbench projection entry point."""

    def __init__(self, *, read_scope: ProjectionReadScope) -> None:
        self._read_scope = read_scope

    def sync(self, *, review_id: str, company_id: int) -> ProjectionSyncResult:
        """Project the committed Hub state to Odoo. Never raises for projection failures."""

        return self._run(review_id=review_id, company_id=company_id, apply=True)

    def plan(self, *, review_id: str, company_id: int) -> ProjectionSyncResult:
        """Dry-run: diff the committed Hub state against Odoo with zero writes anywhere."""

        return self._run(review_id=review_id, company_id=company_id, apply=False)

    def build_projection(self, *, review_id: str, company_id: int) -> WorkbenchProjection:
        """Build the complete projection snapshot from committed Hub records only."""

        with self._read_scope() as sources:
            return _build_projection(sources, review_id=review_id, company_id=company_id)

    def _run(self, *, review_id: str, company_id: int, apply: bool) -> ProjectionSyncResult:
        try:
            # The read scope is opened, used and closed here, in the calling thread.
            with self._read_scope() as sources:
                projection = _build_projection(sources, review_id=review_id, company_id=company_id)
                result = sources.publisher.sync_projection(projection, apply=apply)
        except Exception as exc:  # noqa: BLE001 - never propagate into a committed business transition
            error = _safe_error(exc)
            logger.error(
                "workbench.projection.sync_failed",
                extra={"review_id": review_id, "company_id": company_id, "apply": apply, "error": error},
                # Expected, classified failures (Odoo unreachable, unrepresentable value) carry
                # a safe message; only an unexpected exception needs its traceback.
                exc_info=not _is_classified(exc),
            )
            return ProjectionSyncResult(
                review_id=review_id, outcome=ProjectionSyncOutcome.ERROR, applied=False, error=error
            )
        if result.failed:
            logger.error(
                "workbench.projection.sync_failed",
                extra={"review_id": review_id, "company_id": company_id, "apply": apply, "error": result.error},
            )
        else:
            logger.info(
                "workbench.projection.synced",
                extra={
                    "review_id": review_id,
                    "company_id": company_id,
                    "apply": apply,
                    "outcome": result.outcome.value,
                    "odoo_record_id": result.odoo_record_id,
                },
            )
        return result


def _build_projection(sources: WorkbenchProjectionSources, *, review_id: str, company_id: int) -> WorkbenchProjection:
    """The complete snapshot, from committed Hub records only.

    No live Odoo product/partner resolution: the effective decision state comes from
    the persisted accepted decision and its Stage-2 execution evidence.
    """

    review = sources.review_reader.get_review_item(ReviewDetailQuery(review_id=review_id, company_id=company_id))
    decision, source, effective_state_error = _accepted_state(sources, review, company_id=company_id)
    projection = WorkbenchProjection(
        review_id=review.review_id,
        company_id=company_id,
        invoice_id=review.invoice_id,
        version=review.version,
        status=review.status,
        invoice_number=review.invoice_number,
        supplier_name=review.supplier_name,
        supplier_tax_number=review.supplier_tax_number,
        invoice_date=review.invoice_date,
        currency=review.currency,
        total_amount=review.total_amount,
        # A decided review shows what the accepted decision selected; the Hub
        # review.workflow itself is never rewritten to make the projection look right.
        workflow=(
            decision.selected_workflow
            if decision is not None and decision.selected_workflow is not None
            else review.workflow
        ),
        review_reasons=review.review_reasons,
        warnings=review.warnings,
        updated_at=review.updated_at,
        review_reasons_role=review_reasons_role(review.status),
        accepted_decision=_decision_projection(decision) if decision is not None else None,
        effective_resolutions=_line_resolutions(source) if source is not None else (),
        effective_state_error=effective_state_error,
        execution=_execution(sources, review, decision, company_id=company_id),
        classification_review_version=(
            decision.decision_version - 1 if decision is not None and decision.decision_version > 1 else review.version
        ),
    )
    if sources.guidance_facts_reader is None:
        return projection
    facts = sources.guidance_facts_reader(review, company_id)
    return dataclasses.replace(projection, operator_guidance=_guidance(projection, facts))


def _guidance(projection: WorkbenchProjection, facts: OperatorGuidanceFacts) -> WorkbenchOperatorGuidance:
    decision = projection.accepted_decision
    execution = projection.execution
    return build_operator_guidance(
        GuidanceInput(
            status=projection.status,
            reason_codes=tuple(reason.code for reason in projection.review_reasons),
            supplier_name=projection.supplier_name,
            decision_type=decision.decision_type if decision is not None else None,
            decision_workflow=decision.selected_workflow if decision is not None else None,
            decision_version=decision.decision_version if decision is not None else None,
            execution_state=execution.state if execution is not None else None,
            vendor_bill_id=execution.vendor_bill_id if execution is not None else None,
        ),
        facts,
    )


def _accepted_state(
    sources: WorkbenchProjectionSources, review: ReviewItem, *, company_id: int
) -> tuple[AcceptedReviewDecision | None, ExecutionSourceInvoice | None, str | None]:
    # Same rule as PR #197's ReviewEvidenceReader: only a decision whose
    # review_version_after is the review's current version governs it.
    if review.status is ReviewStatus.PENDING_REVIEW or review.version <= 1:
        return None, None, None
    try:
        decision = sources.accepted_decision_reader.get_accepted_decision(
            review_id=review.review_id, company_id=company_id, decision_version=review.version
        )
    except ReviewNotFoundError:
        return None, None, None
    except ACCEPTED_EVIDENCE_INTEGRITY_ERRORS:
        return None, None, EFFECTIVE_STATE_UNAVAILABLE
    try:
        source = sources.accepted_source_reader.get_source_invoice(
            review_id=review.review_id, company_id=company_id, decision_version=decision.decision_version
        )
    except ExecutionSourceInvoiceNotFoundError:
        # Normal for DISMISS and non-Vendor-Bill decisions: no execution evidence is pinned.
        return decision, None, None
    except ACCEPTED_EVIDENCE_INTEGRITY_ERRORS:
        return decision, None, EFFECTIVE_STATE_UNAVAILABLE
    return decision, source, None


def _execution(
    sources: WorkbenchProjectionSources,
    review: ReviewItem,
    decision: AcceptedReviewDecision | None,
    *,
    company_id: int,
) -> WorkbenchProjectionExecution | None:
    if decision is None:
        return None
    snapshot = sources.execution_snapshot_reader.find_latest_snapshot_for_review(
        review_id=review.review_id, company_id=company_id
    )
    if snapshot is None or snapshot.decision_version != decision.decision_version:
        return None
    return _execution_projection(snapshot)


def _decision_projection(decision: AcceptedReviewDecision) -> WorkbenchProjectionDecision:
    return WorkbenchProjectionDecision(
        decision_version=decision.decision_version,
        decision_type=decision.decision_type,
        selected_workflow=decision.selected_workflow,
    )


def _line_resolutions(source: ExecutionSourceInvoice) -> tuple[WorkbenchProjectionLineResolution, ...]:
    return tuple(
        WorkbenchProjectionLineResolution(
            line_number=line_number,
            kind=resolution.kind.value,
            product_id=resolution.product_id,
            product_source=resolution.product_source.value if resolution.product_source is not None else None,
            expense_account_id=resolution.expense_account_id,
            asset_account_id=resolution.asset_account_id,
            depreciation_model_id=resolution.depreciation_model_id,
        )
        for line_number, resolution in effective_resolutions(source).items()
    )


def _execution_projection(snapshot: ExecutionSnapshot) -> WorkbenchProjectionExecution:
    vendor_bills = [
        artifact
        for step in snapshot.steps
        if step.last_result is not None
        for artifact in step.last_result.produced_artifacts
        if artifact.artifact_type is ExecutionArtifactType.VENDOR_BILL
    ]
    vendor_bill = vendor_bills[0] if len(vendor_bills) == 1 else None
    vendor_bill_id = _positive_int(vendor_bill.artifact_id) if vendor_bill is not None else None
    retry_count = max((step.retry_count for step in snapshot.steps), default=0)
    return WorkbenchProjectionExecution(
        execution_id=snapshot.execution_id,
        decision_version=snapshot.decision_version,
        state=snapshot.state.value,
        vendor_bill_id=vendor_bill_id,
        vendor_bill_external_identity=vendor_bill.external_identity if vendor_bill_id is not None else None,
        retry_count=retry_count,
        max_attempts=snapshot.retry_policy.max_attempts,
        failure_message=snapshot.failure.safe_message if snapshot.failure is not None else None,
    )


def _positive_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _safe_error(exc: Exception) -> str:
    """The safe messages along the cause chain, e.g. "...lookup failed. Odoo returned HTTP 303."."""

    messages: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(messages) < 3:
        message = getattr(current, "safe_message", None)
        if not (isinstance(message, str) and message.strip()) and isinstance(current, WorkbenchContractError):
            message = str(current)
        if isinstance(message, str) and message.strip() and message.strip() not in messages:
            messages.append(message.strip())
        current = current.__cause__
    return " ".join(messages) if messages else SAFE_PROJECTION_SYNC_ERROR


def _is_classified(exc: Exception) -> bool:
    return isinstance(exc, ApplicationError) or isinstance(getattr(exc, "safe_message", None), str)


__all__ = [
    "PROJECTION_SYNC_WARNING",
    "ProjectionFieldChange",
    "ProjectionReadScope",
    "ProjectionSyncOutcome",
    "ProjectionSyncResult",
    "ReviewProjectionSynchronizer",
    "WorkbenchProjectionSources",
    "WorkbenchProjectionSyncPublisher",
    "WorkbenchProjectionSynchronizer",
    "projection_sync_warnings",
    "sync_after_commit",
]
