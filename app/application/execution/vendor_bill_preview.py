"""Read-only Vendor Bill preview (P0-PROD-09B).

Lets an authorized operator see the exact Vendor Bill economics and routing that
EXECUTE would produce, before any Odoo write occurs -- reusing the *same* persisted
accepted evidence and the *same* billing/building logic real execution uses:

    accepted decision (AcceptedReviewDecisionReader, same reader execution uses)
    -> persisted Stage-2 execution evidence (ExecutionSourceInvoiceReader, same reader)
    -> ExecutionPlanner.plan() (same planner, to derive the VENDOR_BILL step + its
       step_key, so the idempotency identity below is byte-identical to execution's)
    -> VendorBillBuilder.build() (same builder, same call shape as
       VendorBillExecutionStrategy.execute())
    -> read-only res.currency resolution (mirrors the same currency resolution real
       execution's Odoo writer already performs before every Vendor Bill write)
    -> VendorBillPreview DTO

This module performs zero current-time business matching (no partner/product/tax
re-resolution -- every match comes from the pinned Stage-2 evidence), computes no
account/product selection of its own (account_only/selected_product_id come from
``source.line_resolutions``, pinned at decision-acceptance time), and holds no
reference to any Odoo write port -- its only ERP dependency type is a narrow,
structurally read-only currency reader (see ``VendorBillPreviewCurrencyReader``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol

from app.application.commands import Command
from app.application.dto import ApplicationDTO
from app.application.exceptions import ApplicationError
from app.application.execution.accepted_decision_use_cases import (
    RunAcceptedDecisionExecutionCommand,
    accepted_decision_execution_id,
)
from app.application.execution.contracts import (
    ExecutionMode,
    ExecutionRequest,
    ExecutionStepRequest,
    ExecutionStepType,
)
from app.application.execution.exceptions import (
    ExecutionPlanningError,
    ExecutionPreviewCurrencyResolutionError,
    ExecutionPreviewUnsupportedWorkflowError,
)
from app.application.execution.planner import ExecutionPlanner
from app.application.execution.ports import AcceptedReviewDecisionReader, ExecutionSourceInvoiceReader
from app.application.execution.vendor_bill_strategy import (
    account_only_line_resolution,
    vendor_bill_write_idempotency_key,
)
from app.application.workflow import WorkflowType
from app.billing import VendorBillBuilder, line_gross_total, line_net_total, line_total_discount

TAX_AMOUNT_PRECISION = Decimal("0.01")


class VendorBillPreviewCurrencyReader(Protocol):
    """Structurally read-only: the only Odoo call the preview path can make.

    Deliberately narrower than any write-capable port -- no create/write/unlink
    method exists on this type at all. See composition for the concrete
    read-only adapter this is satisfied by.
    """

    def resolve_vendor_bill_currency_id(self, currency_code: str) -> int:
        pass


@dataclass(frozen=True, slots=True)
class PreviewVendorBillRequest(Command):
    review_id: str
    company_id: int
    decision_version: int

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.decision_version, "decision_version must be positive.")


@dataclass(frozen=True, slots=True)
class VendorBillPreviewLine(ApplicationDTO):
    line_number: str | None
    description: str | None
    quantity: Decimal
    unit_price: Decimal
    account_id: int | None
    product_id: int | None
    tax_ids: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class VendorBillPreview(ApplicationDTO):
    review_id: str
    company_id: int
    decision_version: int
    decision_id: str | None
    selected_workflow: WorkflowType

    # Vendor Bill header, exactly as EXECUTE would build it.
    move_type: str
    partner_id: int
    invoice_date: date
    reference: str | None
    header_company_id: int | None
    currency_code: str
    currency_id: int

    # The exact idempotency identity a later EXECUTE would use for this Vendor Bill step.
    idempotency_key: str

    lines: tuple[VendorBillPreviewLine, ...]

    # Deterministic PREVIEW totals, computed before any Odoo account.move exists --
    # never Odoo-computed values. gross_source_amount/total_discount are exposed so an
    # operator can see why preview_untaxed differs from the source invoice's own gross.
    gross_source_amount: Decimal
    total_discount: Decimal
    preview_untaxed: Decimal
    preview_tax: Decimal
    preview_total: Decimal


class PreviewVendorBillUseCase:
    """Zero-write Vendor Bill preview from persisted accepted evidence (P0-PROD-09B)."""

    def __init__(
        self,
        *,
        accepted_decision_reader: AcceptedReviewDecisionReader,
        source_invoice_reader: ExecutionSourceInvoiceReader,
        execution_planner: ExecutionPlanner,
        vendor_bill_builder: VendorBillBuilder,
        currency_reader: VendorBillPreviewCurrencyReader,
    ) -> None:
        self._accepted_decision_reader = accepted_decision_reader
        self._source_invoice_reader = source_invoice_reader
        self._execution_planner = execution_planner
        self._vendor_bill_builder = vendor_bill_builder
        self._currency_reader = currency_reader

    def preview(self, request: PreviewVendorBillRequest) -> VendorBillPreview:
        if not isinstance(request, PreviewVendorBillRequest):
            raise ExecutionPlanningError("PreviewVendorBillRequest is required.")

        decision = self._accepted_decision_reader.get_accepted_decision(
            review_id=request.review_id,
            company_id=request.company_id,
            decision_version=request.decision_version,
        )
        if decision.selected_workflow is not WorkflowType.VENDOR_BILL:
            raise ExecutionPreviewUnsupportedWorkflowError(
                "Vendor Bill preview requires an accepted decision whose selected workflow is VENDOR_BILL."
            )

        # Same execution_id/plan construction execution itself uses (accepted_decision_execution_id,
        # ExecutionPlanner.plan) -- DRY_RUN only, never EXECUTE: preview never requires or checks
        # approval, and the mode value plays no part in the per-step idempotency key below.
        preview_command = RunAcceptedDecisionExecutionCommand(
            review_id=decision.review_id,
            company_id=decision.company_id,
            decision_version=decision.decision_version,
            mode=ExecutionMode.DRY_RUN,
        )
        execution_id = accepted_decision_execution_id(preview_command, decision=decision)
        plan = self._execution_planner.plan(
            ExecutionRequest(
                execution_id=execution_id,
                review_id=decision.review_id,
                company_id=decision.company_id,
                decision_version=decision.decision_version,
                decision_id=decision.decision_id,
                idempotency_key=None,
                mode=ExecutionMode.DRY_RUN,
                selected_workflow=decision.selected_workflow,
                business_context_allocations=decision.business_context_allocations,
                selected_quotation_scenario_ids=decision.selected_quotation_scenario_ids,
            )
        )
        vendor_bill_step = next(
            (step for step in plan.steps if step.step_type is ExecutionStepType.VENDOR_BILL),
            None,
        )
        if vendor_bill_step is None:
            raise ExecutionPreviewUnsupportedWorkflowError(
                "Accepted decision produced no VENDOR_BILL execution step to preview."
            )

        source = self._source_invoice_reader.get_source_invoice(
            review_id=decision.review_id,
            company_id=decision.company_id,
            decision_version=decision.decision_version,
        )

        account_only_line_numbers, explicit_account_only_accounts = account_only_line_resolution(
            source.line_resolutions
        )
        vendor_bill = self._vendor_bill_builder.build(
            source.invoice,
            source.partner_match,
            source.product_match,
            source.tax_match,
            company_id=decision.company_id,
            operating_expense_match=source.operating_expense_match,
            account_only_line_numbers=account_only_line_numbers,
            account_only_expense_match=source.account_only_expense_match,
            explicit_account_only_accounts=explicit_account_only_accounts,
        )

        step_request = ExecutionStepRequest(
            execution_id=execution_id,
            review_id=decision.review_id,
            company_id=decision.company_id,
            decision_version=decision.decision_version,
            mode=ExecutionMode.DRY_RUN,
            step=vendor_bill_step,
            approval=None,
            decision_id=decision.decision_id,
        )
        idempotency_key = vendor_bill_write_idempotency_key(step_request)

        # Translate any ERP-layer currency-lookup failure into a pure application-layer
        # exception here -- callers (including the API router) never need to depend on
        # any ERP-layer exception type to handle a preview currency failure.
        try:
            currency_id = self._currency_reader.resolve_vendor_bill_currency_id(vendor_bill.currency)
        except ApplicationError as exc:
            raise ExecutionPreviewCurrencyResolutionError(exc.safe_message) from exc

        lines = tuple(
            VendorBillPreviewLine(
                line_number=source_line.line_number,
                description=bill_line.description,
                quantity=bill_line.quantity,
                unit_price=bill_line.unit_price,
                account_id=bill_line.account_id,
                product_id=bill_line.product_id,
                tax_ids=bill_line.tax_ids,
            )
            for source_line, bill_line in zip(source.invoice.lines, vendor_bill.invoice_lines, strict=True)
        )

        gross_source_amount = sum((line_gross_total(line) for line in source.invoice.lines), Decimal("0"))
        total_discount = sum((line_total_discount(line) for line in source.invoice.lines), Decimal("0"))
        preview_untaxed = sum((line_net_total(line) for line in source.invoice.lines), Decimal("0"))
        preview_tax = sum(
            (
                line_net_total(line) * (tax.rate / Decimal(100))
                for line in source.invoice.lines
                for tax in line.taxes
                if tax.rate is not None
            ),
            Decimal("0"),
        ).quantize(TAX_AMOUNT_PRECISION)
        preview_total = preview_untaxed + preview_tax

        return VendorBillPreview(
            review_id=decision.review_id,
            company_id=decision.company_id,
            decision_version=decision.decision_version,
            decision_id=decision.decision_id,
            selected_workflow=decision.selected_workflow,
            move_type="in_invoice",
            partner_id=vendor_bill.supplier_id,
            invoice_date=vendor_bill.invoice_date,
            reference=vendor_bill.reference,
            header_company_id=vendor_bill.company_id,
            currency_code=vendor_bill.currency,
            currency_id=currency_id,
            idempotency_key=idempotency_key,
            lines=lines,
            gross_source_amount=gross_source_amount,
            total_discount=total_discount,
            preview_untaxed=preview_untaxed,
            preview_tax=preview_tax,
            preview_total=preview_total,
        )


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise ExecutionPlanningError(message)


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise ExecutionPlanningError(message)
