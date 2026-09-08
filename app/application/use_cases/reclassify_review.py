"""Non-destructive deterministic reclassification of an existing Workbench review.

Reclassification loads the review's immutable ``ReviewSourceInvoiceEvidence``,
reruns the *same* normal ``DecisionEngine`` used by first-time import against
current master data, and advances the review projection to version N+1 while
preserving the previous state in an immutable ``WorkbenchReviewReclassification``
event. It is not a human decision, performs no ERP write, and never re-fetches
Uyumsoft or accepts an invoice from the caller.
"""

from __future__ import annotations

from app.application.commands import ImportInvoiceCommand
from app.application.decision import DecisionEngine
from app.application.dto import DecisionResult
from app.application.exceptions import ApplicationError
from app.application.use_cases.review_classification_outcome import (
    build_review_classification_evidence,
    build_review_execution_evidence,
)
from app.application.workbench.exceptions import ReviewPersistenceError, WorkbenchContractError
from app.application.workbench.ports import (
    ReviewReclassificationWriter,
    ReviewSourceInvoiceEvidenceReader,
)
from app.application.workbench.reclassification import (
    ReclassifyReviewCommand,
    ReviewReclassificationProposal,
    ReviewReclassificationResult,
)

SAFE_RECLASSIFICATION_ERROR = "Deterministic review reclassification failed."


class ReclassifyWorkbenchReviewUseCase:
    """Application boundary for one non-destructive review reclassification."""

    def __init__(
        self,
        *,
        decision_engine: DecisionEngine,
        source_invoice_reader: ReviewSourceInvoiceEvidenceReader,
        reclassification_writer: ReviewReclassificationWriter,
    ) -> None:
        self._decision_engine = decision_engine
        self._source_invoice_reader = source_invoice_reader
        self._reclassification_writer = reclassification_writer

    async def execute(self, command: ReclassifyReviewCommand) -> ReviewReclassificationResult:
        if not isinstance(command, ReclassifyReviewCommand):
            raise WorkbenchContractError("ReclassifyReviewCommand is required.")

        # Source of truth: the immutable snapshot only. Never Uyumsoft, never the caller.
        source = self._source_invoice_reader.get(
            review_id=command.review_id,
            company_id=command.company_id,
        )

        decision_result = await self._decide(
            ImportInvoiceCommand(
                invoice=source.invoice,
                idempotency_key=_reclassification_idempotency_key(command),
                company_id=command.company_id,
            )
        )

        to_version = command.expected_version + 1
        classification_evidence = build_review_classification_evidence(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=to_version,
            decision_result=decision_result,
        )
        execution_evidence = build_review_execution_evidence(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=to_version,
            invoice=source.invoice,
            decision_result=decision_result,
        )
        matched_rule_code = classification_evidence.matched_rule_code if classification_evidence is not None else None
        matched_rule_id = classification_evidence.matched_rule_id if classification_evidence is not None else None

        proposal = ReviewReclassificationProposal(
            review_id=command.review_id,
            company_id=command.company_id,
            expected_version=command.expected_version,
            trigger=command.trigger,
            note=command.note,
            source_invoice_id=source.source_invoice_id,
            new_workflow=decision_result.workflow,
            new_review_reasons=decision_result.review_reasons,
            new_warnings=decision_result.warnings,
            matched_rule_code=matched_rule_code,
            matched_rule_id=matched_rule_id,
            new_classification_evidence=classification_evidence,
            new_execution_evidence=execution_evidence,
        )
        return self._reclassification_writer.reclassify_review(proposal)

    async def _decide(self, command: ImportInvoiceCommand) -> DecisionResult:
        try:
            return await self._decision_engine.decide(command)
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001 - translated to a safe application error
            raise ReviewPersistenceError(SAFE_RECLASSIFICATION_ERROR) from exc


def _reclassification_idempotency_key(command: ReclassifyReviewCommand) -> str:
    # The recommendation/manual-review strategies never persist or write this key;
    # it exists only to satisfy the ImportInvoiceCommand contract.
    return f"reclassify:{command.company_id}:{command.review_id}:{command.expected_version}"
