"""Non-destructive deterministic reclassification of an existing Workbench review.

Reclassification loads the review's immutable ``ReviewSourceInvoiceEvidence``,
reruns the *same* normal ``DecisionEngine`` used by first-time import against
current master data, and advances the review projection to version N+1 while
preserving the previous state in an immutable ``WorkbenchReviewReclassification``
event. It is not a human decision, performs no ERP write, and never re-fetches
Uyumsoft or accepts an invoice from the caller.

The "raw decision + accepted review-scoped effects -> effective decision" logic
itself (P0-PROD-10D/15N/15P/15T) lives in
:mod:`app.application.use_cases.effective_decision` -- see that module's docstring
for the full history and rationale of each override. This use case's only job is to
turn one :class:`~app.application.use_cases.effective_decision.EffectiveDecision`
into a version-advancing :class:`ReviewReclassificationProposal`.

P0-PROD-15Z: :class:`~app.application.workbench.execution_evidence_recovery_use_cases.
RebuildReviewExecutionEvidenceUseCase` reuses the exact same
:class:`~app.application.use_cases.effective_decision.EffectiveDecisionResolver` to
repair a review whose accepted business state (supplier/purpose/accounting
resolution) already resolves cleanly but whose derived
``WorkbenchReviewExecutionEvidence`` is missing or stale (e.g. because it was
computed before a fix like P0-PROD-15X was deployed) -- never a second, drifting
implementation of this same computation.
"""

from __future__ import annotations

from app.application.decision import DecisionEngine
from app.application.expense_mapping.matcher import OperatingExpenseMatcher
from app.application.use_cases.effective_decision import SAFE_EFFECTIVE_DECISION_ERROR, EffectiveDecisionResolver
from app.application.use_cases.review_classification_outcome import (
    build_review_classification_evidence,
    build_review_execution_evidence,
)
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workbench.ports import (
    ReviewAccountingResolutionReader,
    ReviewReclassificationWriter,
    ReviewSourceInvoiceEvidenceReader,
    SupplierRemediationEffectWriter,
)
from app.application.workbench.reclassification import (
    ReclassifyReviewCommand,
    ReviewReclassificationProposal,
    ReviewReclassificationResult,
)

#: Preserved as a public re-export: the underlying string now lives in
#: ``effective_decision.SAFE_EFFECTIVE_DECISION_ERROR`` (P0-PROD-15Z extraction).
SAFE_RECLASSIFICATION_ERROR = SAFE_EFFECTIVE_DECISION_ERROR


class ReclassifyWorkbenchReviewUseCase:
    """Application boundary for one non-destructive review reclassification."""

    def __init__(
        self,
        *,
        decision_engine: DecisionEngine,
        source_invoice_reader: ReviewSourceInvoiceEvidenceReader,
        reclassification_writer: ReviewReclassificationWriter,
        supplier_remediation_effect_reader: SupplierRemediationEffectWriter | None = None,
        operating_expense_matcher: OperatingExpenseMatcher | None = None,
        review_accounting_resolution_reader: ReviewAccountingResolutionReader | None = None,
    ) -> None:
        self._reclassification_writer = reclassification_writer
        self._resolver = EffectiveDecisionResolver(
            decision_engine=decision_engine,
            source_invoice_reader=source_invoice_reader,
            supplier_remediation_effect_reader=supplier_remediation_effect_reader,
            operating_expense_matcher=operating_expense_matcher,
            review_accounting_resolution_reader=review_accounting_resolution_reader,
        )

    async def execute(self, command: ReclassifyReviewCommand) -> ReviewReclassificationResult:
        if not isinstance(command, ReclassifyReviewCommand):
            raise WorkbenchContractError("ReclassifyReviewCommand is required.")

        effective = await self._resolver.resolve(
            review_id=command.review_id,
            company_id=command.company_id,
            idempotency_key=_reclassification_idempotency_key(command),
        )

        to_version = command.expected_version + 1
        classification_evidence = build_review_classification_evidence(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=to_version,
            decision_result=effective.decision_result,
        )
        execution_evidence = build_review_execution_evidence(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=to_version,
            invoice=effective.source.invoice,
            decision_result=effective.execution_decision_result,
        )
        matched_rule_code = classification_evidence.matched_rule_code if classification_evidence is not None else None
        matched_rule_id = classification_evidence.matched_rule_id if classification_evidence is not None else None

        proposal = ReviewReclassificationProposal(
            review_id=command.review_id,
            company_id=command.company_id,
            expected_version=command.expected_version,
            trigger=command.trigger,
            note=command.note,
            source_invoice_id=effective.source.source_invoice_id,
            new_workflow=effective.effective_workflow,
            new_review_reasons=effective.effective_review_reasons,
            new_warnings=effective.decision_result.warnings,
            matched_rule_code=matched_rule_code,
            matched_rule_id=matched_rule_id,
            new_classification_evidence=classification_evidence,
            new_execution_evidence=execution_evidence,
        )
        return self._reclassification_writer.reclassify_review(proposal)


def _reclassification_idempotency_key(command: ReclassifyReviewCommand) -> str:
    # The recommendation/manual-review strategies never persist or write this key;
    # it exists only to satisfy the ImportInvoiceCommand contract.
    return f"reclassify:{command.company_id}:{command.review_id}:{command.expected_version}"


__all__ = ["SAFE_RECLASSIFICATION_ERROR", "ReclassifyWorkbenchReviewUseCase"]
