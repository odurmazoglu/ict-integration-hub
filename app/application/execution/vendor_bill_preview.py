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

P0-PROD-18F-1: for a decision accepted under a RESALE purchase purpose, each line also
shows the RESALE accounting pinned *at decision acceptance* (``resale_accounting``) --
loaded from Hub persistence only, never from Odoo's current accounting configuration,
so it stays stable if Odoo changes later. A RESALE decision without a valid pin fails
closed.

P0-PROD-18F-2: EXECUTE now sends the pinned account explicitly on each RESALE product
line, so a RESALE line's ``account_id`` shows that pinned account (still read only from
the pin). EXECUTE sends it only after its own pre-write drift and fiscal-position
checks pass -- preview does not run those Odoo reads; if they fail, EXECUTE fails
closed and creates nothing.
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
    ExecutionPreviewProductUomResolutionError,
    ExecutionPreviewResaleAccountingError,
    ExecutionPreviewUnsupportedWorkflowError,
    ExecutionSourceInvoiceIntegrityError,
)
from app.application.execution.planner import ExecutionPlanner
from app.application.execution.ports import AcceptedReviewDecisionReader, ExecutionSourceInvoiceReader
from app.application.execution.vendor_bill_strategy import (
    account_only_line_resolution,
    vendor_bill_write_idempotency_key,
)
from app.application.workbench.purchase_account_discovery import FiscalPositionMapping
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workbench.resale_accounting_pin import ResaleAccountingPin, ResaleAccountingSource
from app.application.workbench.resale_decision_gate import PurchasePurposeHistoryReader
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


class VendorBillPreviewProductUomReader(Protocol):
    """Structurally read-only (P0-PROD-10E): the only other Odoo call the preview
    path can make, mirroring ``VendorBillPreviewCurrencyReader`` exactly. No
    create/write/unlink method exists on this type at all.
    """

    def resolve_vendor_bill_product_uom_ids(self, product_ids: tuple[int, ...]) -> dict[int, int]:
        pass


class VendorBillPreviewResaleAccountingPinReader(Protocol):
    """Hub-persistence-only read of an accepted decision's RESALE pin (P0-PROD-18F-1). No Odoo access."""

    def get_resale_accounting_pin(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
    ) -> ResaleAccountingPin | None:
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
class VendorBillPreviewResaleAccounting(ApplicationDTO):
    """The RESALE accounting pinned at decision acceptance for one line (P0-PROD-18F-1).

    A pre-fiscal-position account only; never the final Vendor Bill line account.
    """

    product_id: int
    product_categ_id: int
    product_categ_name: str | None
    account_id: int
    account_code: str
    account_name: str
    account_type: str
    accounting_source: ResaleAccountingSource
    fiscal_position_mapping: FiscalPositionMapping


@dataclass(frozen=True, slots=True)
class VendorBillPreviewLine(ApplicationDTO):
    line_number: str | None
    description: str | None
    quantity: Decimal
    unit_price: Decimal
    account_id: int | None
    product_id: int | None
    tax_ids: tuple[int, ...] = field(default_factory=tuple)
    # P0-PROD-10E: the exact Odoo uom_id EXECUTE would write for this line -- None
    # for an account-only line (no product, no UoM), never the source invoice's own
    # UN/CEFACT unit code.
    product_uom_id: int | None = None
    # P0-PROD-18F-1: set only for a RESALE decision, from its immutable pin.
    resale_accounting: VendorBillPreviewResaleAccounting | None = None


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
        product_uom_reader: VendorBillPreviewProductUomReader,
        resale_accounting_pin_reader: VendorBillPreviewResaleAccountingPinReader | None = None,
        purchase_purpose_reader: PurchasePurposeHistoryReader | None = None,
    ) -> None:
        self._accepted_decision_reader = accepted_decision_reader
        self._source_invoice_reader = source_invoice_reader
        self._execution_planner = execution_planner
        self._vendor_bill_builder = vendor_bill_builder
        self._currency_reader = currency_reader
        self._product_uom_reader = product_uom_reader
        self._resale_accounting_pin_reader = resale_accounting_pin_reader
        self._purchase_purpose_reader = purchase_purpose_reader

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
        resale_accounting_by_line = self._pinned_resale_accounting(decision, source, vendor_bill)

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

        # P0-PROD-10E: resolve the real Odoo uom_id for every product on this bill,
        # via the same read-only, fail-closed path EXECUTE's writer uses -- never the
        # source invoice's own UN/CEFACT unit code. A preview must fail exactly when
        # EXECUTE would fail, not merely display a wrong or missing value.
        product_ids = tuple(
            sorted(
                {bill_line.product_id for bill_line in vendor_bill.invoice_lines if bill_line.product_id is not None}
            )
        )
        try:
            product_uom_ids = self._product_uom_reader.resolve_vendor_bill_product_uom_ids(product_ids)
        except ApplicationError as exc:
            raise ExecutionPreviewProductUomResolutionError(exc.safe_message) from exc

        lines = tuple(
            VendorBillPreviewLine(
                line_number=source_line.line_number,
                description=bill_line.description,
                quantity=bill_line.quantity,
                unit_price=bill_line.unit_price,
                account_id=_preview_account_id(
                    bill_line.account_id, resale_accounting_by_line.get(source_line.line_number)
                ),
                product_id=bill_line.product_id,
                tax_ids=bill_line.tax_ids,
                product_uom_id=product_uom_ids.get(bill_line.product_id) if bill_line.product_id is not None else None,
                resale_accounting=resale_accounting_by_line.get(source_line.line_number),
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

    def _pinned_resale_accounting(
        self, decision, source, vendor_bill
    ) -> dict[str | None, VendorBillPreviewResaleAccounting]:
        """The decision's own RESALE pin per line -- Hub persistence only, fail closed.

        RESALE-ness is the purchase purpose of the version the decision was accepted
        on (``decision_version - 1``), exactly what the decision gate evaluated.
        """

        if self._resale_accounting_pin_reader is None or self._purchase_purpose_reader is None:
            return {}
        accepted_on_version = decision.decision_version - 1
        is_resale = self._accepted_under_resale(decision, accepted_on_version)
        try:
            pin = self._resale_accounting_pin_reader.get_resale_accounting_pin(
                review_id=decision.review_id,
                company_id=decision.company_id,
                decision_version=decision.decision_version,
            )
        except ExecutionSourceInvoiceIntegrityError as exc:
            raise ExecutionPreviewResaleAccountingError("The RESALE accounting pin is invalid.") from exc
        if not is_resale:
            if pin is not None:
                raise ExecutionPreviewResaleAccountingError(
                    "A RESALE accounting pin exists for a decision not accepted under a RESALE purpose."
                )
            return {}
        if pin is None:
            raise ExecutionPreviewResaleAccountingError(
                "This RESALE decision has no pinned accounting evidence; preview does not fall back to Odoo."
            )
        if pin.review_version != accepted_on_version:
            raise ExecutionPreviewResaleAccountingError("The RESALE accounting pin belongs to another review version.")
        pinned_lines = pin.by_line_number()
        if set(pinned_lines) != {line.line_number for line in source.invoice.lines}:
            raise ExecutionPreviewResaleAccountingError("The RESALE accounting pin does not cover every invoice line.")
        result: dict[str | None, VendorBillPreviewResaleAccounting] = {}
        for source_line, bill_line in zip(source.invoice.lines, vendor_bill.invoice_lines, strict=True):
            pinned = pinned_lines[source_line.line_number]
            if bill_line.product_id != pinned.product_id:
                raise ExecutionPreviewResaleAccountingError("The RESALE accounting pin names a different product.")
            result[source_line.line_number] = VendorBillPreviewResaleAccounting(
                product_id=pinned.product_id,
                product_categ_id=pinned.product_categ_id,
                product_categ_name=pinned.product_categ_name,
                account_id=pinned.pre_fiscal_position_account_id,
                account_code=pinned.pre_fiscal_position_account_code,
                account_name=pinned.pre_fiscal_position_account_name,
                account_type=pinned.pre_fiscal_position_account_type,
                accounting_source=pinned.account_source,
                fiscal_position_mapping=pinned.fiscal_position_mapping,
            )
        return result

    def _accepted_under_resale(self, decision, accepted_on_version: int) -> bool:
        resolutions = self._purchase_purpose_reader.list_purchase_purpose_resolutions(
            review_id=decision.review_id,
            company_id=decision.company_id,
        )
        current = [resolution for resolution in resolutions if resolution.review_version == accepted_on_version]
        if len(current) > 1:
            raise ExecutionPreviewResaleAccountingError(
                "More than one purchase purpose exists for the decision version."
            )
        return bool(current) and current[0].purchase_purpose is PurchasePurpose.RESALE


def _preview_account_id(
    bill_account_id: int | None, resale_accounting: VendorBillPreviewResaleAccounting | None
) -> int | None:
    """The account EXECUTE sends: a RESALE line's pinned account (P0-PROD-18F-2), else the bill's."""

    return resale_accounting.account_id if resale_accounting is not None else bill_account_id


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise ExecutionPlanningError(message)


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise ExecutionPlanningError(message)
