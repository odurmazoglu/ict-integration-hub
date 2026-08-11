from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from app.application.exceptions import ApplicationError
from app.application.workbench.allocations import BusinessContextAllocation, BusinessContextAllocationType
from app.application.workbench.billing_authoring import (
    CaptureOdooWorkbenchBillingEvidenceCommand,
    CaptureOdooWorkbenchBillingEvidenceResult,
    ValidatedWorkbenchBillingAuthoring,
    WorkbenchBillingAuthoringRow,
)
from app.application.workbench.evidence import ReviewExecutionBillingEvidence
from app.application.workbench.exceptions import ReviewDataIntegrityError, WorkbenchContractError
from app.application.workbench.ports import (
    ReviewBillingEvidenceWriter,
    ReviewQueueReader,
    WorkbenchBillingAuthoringReader,
    WorkbenchBillingReferenceValidator,
    WorkbenchDecisionCandidateReader,
)
from app.application.workbench.queries import ReviewDetailQuery
from app.billing.dto import CustomerInvoiceBillingInstruction, CustomerInvoiceBillingLine


class CaptureOdooWorkbenchBillingEvidenceUseCase:
    """Capture Odoo-authored billing terms as immutable Stage 1 evidence."""

    def __init__(
        self,
        *,
        review_reader: ReviewQueueReader,
        candidate_reader: WorkbenchDecisionCandidateReader,
        billing_authoring_reader: WorkbenchBillingAuthoringReader,
        billing_evidence_writer: ReviewBillingEvidenceWriter,
        reference_validator: WorkbenchBillingReferenceValidator,
    ) -> None:
        self._review_reader = review_reader
        self._candidate_reader = candidate_reader
        self._billing_authoring_reader = billing_authoring_reader
        self._billing_evidence_writer = billing_evidence_writer
        self._reference_validator = reference_validator

    def execute(
        self,
        command: CaptureOdooWorkbenchBillingEvidenceCommand,
    ) -> CaptureOdooWorkbenchBillingEvidenceResult:
        if not isinstance(command, CaptureOdooWorkbenchBillingEvidenceCommand):
            raise WorkbenchContractError("CaptureOdooWorkbenchBillingEvidenceCommand is required.")
        review = self._review_reader.get_review_item(
            ReviewDetailQuery(review_id=command.review_id, company_id=command.company_id)
        )
        candidate = _translate_failure(
            lambda: self._candidate_reader.get_ready_decision(
                review_id=command.review_id,
                company_id=command.company_id,
            ),
            "Odoo Workbench review context could not be read safely.",
        )
        if candidate.expected_version != review.version:
            raise ReviewDataIntegrityError("Odoo Workbench review context version is stale.")
        rows = _translate_failure(
            lambda: self._billing_authoring_reader.get_billing_authoring(
                review_id=command.review_id,
                company_id=command.company_id,
            ),
            "Odoo Workbench billing authoring could not be read safely.",
        )
        if not rows:
            raise ReviewDataIntegrityError("Odoo Workbench billing authoring was not found.")
        _validate_rows_scope(
            rows,
            review_id=review.review_id,
            company_id=command.company_id,
            review_version=review.version,
        )
        allocations = candidate.business_context_allocations
        if allocations is None:
            raise ReviewDataIntegrityError("Billing evidence requires allocation context.")
        creation_allocations = {
            allocation.allocation_key: allocation
            for allocation in allocations.allocations
            if allocation.allocation_type is BusinessContextAllocationType.CUSTOMER_RECHARGE
            and allocation.customer_invoice_id is None
        }
        existing_invoice_keys = {
            allocation.allocation_key
            for allocation in allocations.allocations
            if allocation.allocation_type is BusinessContextAllocationType.CUSTOMER_RECHARGE
            and allocation.customer_invoice_id is not None
        }
        _validate_allocation_linkage(
            rows,
            creation_allocations=creation_allocations,
            existing_invoice_keys=existing_invoice_keys,
        )
        validated_authoring = self._reference_validator.validate_billing_authoring(
            rows,
            requested_company_id=command.company_id,
        )
        instructions = _instructions(validated_authoring)
        evidence = tuple(
            ReviewExecutionBillingEvidence(
                review_id=review.review_id,
                company_id=command.company_id,
                review_version=review.version,
                billing_instruction=instruction,
            )
            for instruction in instructions
        )
        persisted = self._billing_evidence_writer.capture_review_billing_evidence(evidence)
        return CaptureOdooWorkbenchBillingEvidenceResult(
            review_id=review.review_id,
            company_id=command.company_id,
            review_version=review.version,
            billing_keys=tuple(item.billing_instruction.billing_key for item in persisted),
        )


def _translate_failure[ResultT](operation: Callable[[], ResultT], fallback_message: str) -> ResultT:
    try:
        return operation()
    except ApplicationError:
        raise
    except Exception as exc:
        raise ReviewDataIntegrityError(fallback_message) from exc


def _validate_rows_scope(
    rows: tuple[WorkbenchBillingAuthoringRow, ...],
    *,
    review_id: str,
    company_id: int,
    review_version: int,
) -> None:
    seen_row_ids: set[int] = set()
    for row in rows:
        if not isinstance(row, WorkbenchBillingAuthoringRow):
            raise ReviewDataIntegrityError("Workbench billing authoring rows must be canonical.")
        if row.odoo_record_id in seen_row_ids:
            raise ReviewDataIntegrityError("Duplicate Odoo billing authoring row identity.")
        seen_row_ids.add(row.odoo_record_id)
        if (row.review_id, row.company_id, row.review_version) != (review_id, company_id, review_version):
            raise ReviewDataIntegrityError("Odoo billing authoring review identity is stale or out of scope.")
        if row.billing_ready is not True:
            raise ReviewDataIntegrityError("Odoo billing authoring is not ready.")


def _validate_allocation_linkage(
    rows: tuple[WorkbenchBillingAuthoringRow, ...],
    *,
    creation_allocations: dict[str, BusinessContextAllocation],
    existing_invoice_keys: set[str],
) -> None:
    covered: list[str] = []
    for row in rows:
        if row.allocation_key in existing_invoice_keys:
            raise ReviewDataIntegrityError("Existing-invoice allocations cannot have creation billing evidence.")
        allocation = creation_allocations.get(row.allocation_key)
        if allocation is None:
            raise ReviewDataIntegrityError("Billing authoring references an unknown creation allocation.")
        if allocation.recharge_partner_id != row.customer_id:
            raise ReviewDataIntegrityError("Billing customer must match allocation recharge_partner_id.")
        covered.append(row.allocation_key)
    if len(set(covered)) != len(covered):
        raise ReviewDataIntegrityError("Billing authoring duplicates allocation coverage.")
    if set(covered) != set(creation_allocations):
        raise ReviewDataIntegrityError("Billing authoring must cover every creation allocation exactly.")


def _instructions(
    validated_authoring: ValidatedWorkbenchBillingAuthoring,
) -> tuple[CustomerInvoiceBillingInstruction, ...]:
    grouped: dict[str, list[WorkbenchBillingAuthoringRow]] = defaultdict(list)
    for row in validated_authoring.rows:
        grouped[row.billing_group_key].append(row)
    instructions: list[CustomerInvoiceBillingInstruction] = []
    for billing_key in sorted(grouped):
        group_rows = tuple(
            sorted(
                grouped[billing_key],
                key=lambda row: (row.sequence or 0, row.allocation_key, row.odoo_record_id),
            )
        )
        customer_ids = {row.customer_id for row in group_rows}
        currency_ids = {row.currency_id for row in group_rows}
        currency_codes = {validated_authoring.currency_code_for(row.currency_id) for row in group_rows}
        if len(customer_ids) != 1:
            raise ReviewDataIntegrityError("Billing group customer must be consistent.")
        if len(currency_ids) != 1 or len(currency_codes) != 1:
            raise ReviewDataIntegrityError("Billing group currency must be consistent.")
        instructions.append(
            CustomerInvoiceBillingInstruction(
                billing_key=billing_key,
                customer_id=group_rows[0].customer_id,
                currency=validated_authoring.currency_code_for(group_rows[0].currency_id),
                lines=tuple(
                    CustomerInvoiceBillingLine(
                        allocation_key=row.allocation_key,
                        product_id=row.product_id,
                        description=row.description,
                        quantity=row.quantity,
                        unit_price=row.unit_price,
                        sales_tax_ids=row.sales_tax_ids,
                    )
                    for row in group_rows
                ),
            )
        )
    return tuple(instructions)
