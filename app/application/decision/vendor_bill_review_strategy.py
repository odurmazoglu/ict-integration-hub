from __future__ import annotations

from time import perf_counter

from app.application.commands import ImportInvoiceCommand
from app.application.decision.exceptions import UnsupportedWorkflowError
from app.application.dto import DecisionResult, RuleEvaluationResult
from app.application.workflow import WorkflowType


class VendorBillReviewRecommendationStrategy:
    """Import-decision strategy that routes a matched Vendor Bill into Workbench review.

    A fully deterministically matched incoming supplier invoice is a
    *recommendation* for a direct Vendor Bill decision, not an ERP write. This
    strategy performs no ERP write, no ``VendorBillBuilder`` call, and no Odoo
    writer call. The draft supplier bill is created only later, from a
    human-accepted ``SELECT_WORKFLOW + VENDOR_BILL`` decision, through the
    existing execution runtime and ``VendorBillExecutionStrategy``.
    """

    workflow = WorkflowType.VENDOR_BILL
    name = "vendor_bill_review_recommendation"

    async def execute(self, command: ImportInvoiceCommand, rule_result: RuleEvaluationResult) -> DecisionResult:
        started = perf_counter()
        if rule_result.workflow is not WorkflowType.VENDOR_BILL:
            raise UnsupportedWorkflowError(
                f"VendorBillReviewRecommendationStrategy cannot execute workflow: {rule_result.workflow.value}."
            )
        if rule_result.partner_match is None or rule_result.product_match is None or rule_result.tax_match is None:
            raise UnsupportedWorkflowError("Vendor Bill review recommendation requires matching rule outputs.")
        return DecisionResult(
            success=True,
            invoice_id=command.invoice.header.ettn or command.invoice.header.invoice_uuid,
            workflow=WorkflowType.VENDOR_BILL,
            strategy=self.name,
            status="review_required",
            vendor_bill_id=None,
            review_required=True,
            partner_match=rule_result.partner_match,
            product_match=rule_result.product_match,
            tax_match=rule_result.tax_match,
            warnings=rule_result.warnings,
            errors=rule_result.errors,
            duration=perf_counter() - started,
        )
