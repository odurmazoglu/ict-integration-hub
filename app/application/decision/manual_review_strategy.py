from __future__ import annotations

from time import perf_counter

from app.application.commands import ImportInvoiceCommand
from app.application.decision.exceptions import UnsupportedWorkflowError
from app.application.dto import DecisionResult, RuleEvaluationResult
from app.application.workflow import ManualReviewDecision, WorkflowType


class ManualReviewStrategy:
    """Non-writing workflow strategy for deterministic review-required decisions."""

    workflow = WorkflowType.MANUAL_REVIEW
    name = WorkflowType.MANUAL_REVIEW.value

    async def execute(self, command: ImportInvoiceCommand, rule_result: RuleEvaluationResult) -> DecisionResult:
        started = perf_counter()
        manual_review = _manual_review_decision(rule_result)
        return DecisionResult(
            success=False,
            invoice_id=command.invoice.header.ettn or command.invoice.header.invoice_uuid,
            workflow=WorkflowType.MANUAL_REVIEW,
            strategy=WorkflowType.MANUAL_REVIEW.value,
            status="review_required",
            vendor_bill_id=None,
            review_required=True,
            review_reasons=manual_review.reasons,
            warnings=manual_review.warnings,
            errors=(),
            duration=perf_counter() - started,
        )


def _manual_review_decision(rule_result: RuleEvaluationResult) -> ManualReviewDecision:
    if rule_result.workflow != WorkflowType.MANUAL_REVIEW:
        raise UnsupportedWorkflowError(f"ManualReviewStrategy cannot execute workflow: {rule_result.workflow.value}.")
    manual_review = rule_result.workflow_decision.manual_review
    if manual_review is None or not manual_review.reasons:
        raise UnsupportedWorkflowError("Manual Review workflow requires structured review reasons.")
    return manual_review
