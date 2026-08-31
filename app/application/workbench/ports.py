from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from app.application.workbench.billing_authoring import (
    ValidatedWorkbenchBillingAuthoring,
    WorkbenchBillingAuthoringRow,
)
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.dto import ReviewDecisionAcknowledgement, ReviewItem, ReviewQueueResult
from app.application.workbench.evidence import (
    ReviewClassificationEvidence,
    ReviewExecutionBillingEvidence,
    ReviewExecutionEvidence,
)
from app.application.workbench.projection import (
    OdooWorkbenchDecisionCandidate,
    ProjectionPublishResult,
    WorkbenchProjection,
)
from app.application.workbench.queries import ReviewDetailQuery, ReviewQueueQuery
from app.billing.dto import CustomerInvoiceBillingInstruction

if TYPE_CHECKING:
    from app.application.execution.contracts import ExecutionSourceInvoice


class ReviewQueueReader(Protocol):
    """Read-only port for future Workbench review queue adapters."""

    def list_review_items(self, query: ReviewQueueQuery) -> ReviewQueueResult:
        pass

    def get_review_item(self, query: ReviewDetailQuery) -> ReviewItem:
        pass


class ReviewItemWriter(Protocol):
    """Write port for idempotent creation of pending Workbench review items."""

    def create_review_item(self, item: ReviewItem, *, company_id: int, idempotency_key: str) -> ReviewItem:
        pass

    def create_review_item_with_execution_evidence(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        evidence: ReviewExecutionEvidence,
        classification_evidence: ReviewClassificationEvidence | None = None,
    ) -> ReviewItem:
        pass

    def create_review_item_with_classification_evidence(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        classification_evidence: ReviewClassificationEvidence,
    ) -> ReviewItem:
        pass

    def create_review_item_with_billing_evidence(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        billing_evidence: tuple[ReviewExecutionBillingEvidence, ...],
        classification_evidence: ReviewClassificationEvidence | None = None,
    ) -> ReviewItem:
        pass

    def create_review_item_with_execution_and_billing_evidence(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        evidence: ReviewExecutionEvidence,
        billing_evidence: tuple[ReviewExecutionBillingEvidence, ...],
        classification_evidence: ReviewClassificationEvidence | None = None,
    ) -> ReviewItem:
        pass


class ReviewDecisionWriter(Protocol):
    """Write port for explicit user decision submission against a pending review item."""

    def has_matching_review_decision(self, command: ReviewDecisionCommand) -> bool:
        pass

    def submit_review_decision(self, command: ReviewDecisionCommand) -> ReviewDecisionAcknowledgement:
        pass

    def submit_review_decision_with_execution_evidence(
        self,
        command: ReviewDecisionCommand,
        evidence: ExecutionSourceInvoice,
    ) -> ReviewDecisionAcknowledgement:
        pass

    def submit_review_decision_with_execution_and_billing_evidence(
        self,
        command: ReviewDecisionCommand,
        evidence: ExecutionSourceInvoice,
        billing_instructions: tuple[CustomerInvoiceBillingInstruction, ...],
    ) -> ReviewDecisionAcknowledgement:
        pass


class ReviewExecutionEvidenceReader(Protocol):
    """Read-only port for immutable execution source evidence available at review submission time."""

    def get_evidence(
        self,
        *,
        review_id: str,
        company_id: int,
        expected_version: int,
    ) -> ExecutionSourceInvoice:
        pass


class ReviewBillingEvidenceReader(Protocol):
    """Read-only port for immutable customer billing evidence available at review submission time."""

    def get_billing_instructions(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> tuple[CustomerInvoiceBillingInstruction, ...]:
        pass


class ReviewClassificationEvidenceReader(Protocol):
    """Read-only port for immutable classification evidence pinned to a review version."""

    def get_classification_evidence(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> ReviewClassificationEvidence:
        pass


class ReviewBillingEvidenceWriter(Protocol):
    """Write port for append-only Stage 1 customer billing evidence capture."""

    def capture_review_billing_evidence(
        self,
        billing_evidence: tuple[ReviewExecutionBillingEvidence, ...],
    ) -> tuple[ReviewExecutionBillingEvidence, ...]:
        pass


class WorkbenchBillingAuthoringReader(Protocol):
    """Read-only port for Odoo-authored Customer Invoice billing terms."""

    def get_billing_authoring(
        self,
        *,
        review_id: str,
        company_id: int,
    ) -> tuple[WorkbenchBillingAuthoringRow, ...]:
        pass


class WorkbenchBillingReferenceValidator(Protocol):
    """Read-only exact ERP reference validator for billing authoring rows."""

    def validate_billing_authoring(
        self,
        rows: tuple[WorkbenchBillingAuthoringRow, ...],
        *,
        requested_company_id: int,
    ) -> ValidatedWorkbenchBillingAuthoring:
        pass


class WorkbenchProjectionPublisher(Protocol):
    """Port for publishing Hub-owned review projections to an ERP UI surface."""

    def publish_projection(self, projection: WorkbenchProjection) -> ProjectionPublishResult:
        pass

    def acknowledge_decision(
        self,
        acknowledgement: ReviewDecisionAcknowledgement,
        *,
        odoo_record_id: int,
        trace_id: str | None = None,
        idempotency_key: str | None = None,
        clear_ready: bool = False,
    ) -> ProjectionPublishResult:
        pass


class WorkbenchDecisionCandidateReader(Protocol):
    """Port for reading user-submitted decision candidates from an ERP UI surface."""

    def list_ready_decisions(self, *, company_id: int, limit: int) -> tuple[OdooWorkbenchDecisionCandidate, ...]:
        pass

    def get_ready_decision(self, *, review_id: str, company_id: int) -> OdooWorkbenchDecisionCandidate:
        pass
