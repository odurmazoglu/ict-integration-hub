from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.application.execution import (
    ExecutionArtifactType,
    ExecutionMode,
    ExecutionState,
    WorkbenchVendorBillExecutionStatus,
)
from app.application.expense_mapping import OperatingExpenseMappingOnboardingOutcome
from app.application.quotation import WorkbenchQuotationScenarioEvidenceStatus
from app.application.workbench.accounting_resolution import AccountingResolutionStatus, AccountingTreatmentType
from app.application.workbench.allocations import AllocationCompleteness, BusinessContextAllocationType
from app.application.workbench.decision_ingestion import WorkbenchDecisionIngestionStatus
from app.application.workbench.dto import ReviewDecisionType, ReviewStatus
from app.application.workbench.one_off_vendor_retirement import ArchiveOneOffVendorStatus, OneOffVendorRetirementStatus
from app.application.workbench.operating_expense_mapping_command import OperatingExpenseMappingSubmissionStatus
from app.application.workbench.product_remediation import ProductRemediationStatus
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workbench.supplier_remediation import SupplierPartnerWriteEffectStatus, SupplierRemediationStatus
from app.application.workbench.supplier_resolution import SupplierResolutionMode
from app.application.workbench.write_authorization import WriteAuthorizationOperationType, WriteAuthorizationStatus
from app.application.workflow import ManualReviewReasonCode, WorkflowType


class ApiErrorItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str


class ApiEnvelope[DataT](BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    success: bool
    data: DataT | None
    warnings: list[str] = Field(default_factory=list)
    errors: list[ApiErrorItem] = Field(default_factory=list)
    trace_id: str


class ManualReviewReasonResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    code: ManualReviewReasonCode
    message: str
    line_number: str | None = None
    tax_index: int | None = None
    candidate_count: int | None = None
    source: str | None = None
    details: list[list[str]]


class ReviewItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    review_id: str
    invoice_id: str
    invoice_number: str | None
    supplier_tax_number: str | None
    supplier_name: str | None
    invoice_date: date | None
    currency: str | None
    total_amount: str | None
    workflow: WorkflowType
    status: ReviewStatus
    review_reasons: list[ManualReviewReasonResponse]
    warnings: list[str]
    created_at: datetime | None
    updated_at: datetime | None
    version: int
    evidence: ReviewEvidenceResponse | None = None


class SupplierCandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    partner_id: int
    name: str | None
    vat: str | None
    active: bool
    company_type: str | None
    parent_id: int | None
    commercial_partner_id: int | None
    street: str | None
    street2: str | None
    zip: str | None
    city: str | None
    state_id: int | None
    country_id: int | None
    email: str | None
    phone: str | None
    mobile: str | None
    website: str | None
    supplier_rank: int | None
    customer_rank: int | None
    company_id: int | None


class ProductMatchEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    status: str
    product_id: int | None
    matched_by: str | None
    reason: str
    candidate_count: int
    confidence: str | None


class SourceTaxResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tax_type: str | None
    rate: str | None
    tax_amount: str | None


class SourceLineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    line_number: str | None
    description: str | None
    quantity: str | None
    unit_code: str | None
    unit_price: str | None
    gross_amount: str | None
    discount_amount: str | None
    net_amount: str | None
    taxes: list[SourceTaxResponse]
    seller_item_code: str | None
    buyer_item_code: str | None
    product_match: ProductMatchEvidenceResponse | None


class ReviewEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    supplier_candidates: list[SupplierCandidateResponse]
    source_lines: list[SourceLineResponse]


class ReviewQueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[ReviewItemResponse]
    total_count: int
    limit: int
    offset: int


class LineResolutionRequest(BaseModel):
    """One explicit per-line resolution: a specific Odoo product, or an explicit
    "no product -- post as expense" decision (business label: POST_AS_EXPENSE /
    "Post as Expense" / "Ürün oluşturmadan gider olarak işle"). The internal/domain
    field names stay ``account_only``/``expense_account_id`` -- see
    ``app.application.workbench.dto.LineResolution``, which this mirrors exactly.

    ``expense_account_id`` (P0-PROD-08G) is the operator-confirmed Odoo
    ``account.account`` id for this specific line's expense posting -- required
    whenever ``account_only=true``. This REST contract is deliberately stricter
    than the domain DTO's own validation: every *new* account-only submission must
    name its own account explicitly; there is no bare ``account_only=true`` path
    left reachable from this endpoint (the legacy whole-vendor expense-mapping
    fallback still exists for already-persisted historical decisions -- see
    ``app.billing.builder`` -- it just cannot be freshly initiated here anymore).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    line_number: str
    selected_product_id: int | None = None
    account_only: bool = False
    expense_account_id: int | None = None

    @model_validator(mode="after")
    def _validate_mutually_exclusive_resolution(self) -> LineResolutionRequest:
        if self.account_only:
            if self.selected_product_id is not None:
                raise ValueError("An account-only line resolution must not also select a product.")
            if self.expense_account_id is None:
                raise ValueError("An account-only line resolution requires an explicit expense_account_id.")
        else:
            if self.selected_product_id is None:
                raise ValueError("A line resolution requires either selected_product_id or account_only=true.")
            if self.expense_account_id is not None:
                raise ValueError("expense_account_id is only valid for an account-only line resolution.")
        return self


class TaxResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    line_number: str
    tax_index: int
    selected_tax_id: int


class BusinessContextAllocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allocation_key: str
    allocation_type: BusinessContextAllocationType
    source_line_number: str | None = None
    description: str | None = None
    amount: Decimal | None = None
    percentage: Decimal | None = None
    currency: str | None = None
    customer_id: int | None = None
    recharge_partner_id: int | None = None
    customer_invoice_id: int | None = None
    target_company_id: int | None = None
    opportunity_id: int | None = None
    sales_order_id: int | None = None
    sales_order_line_id: int | None = None
    proposal_scenario_id: int | None = None
    purchase_order_id: int | None = None
    project_id: int | None = None
    analytic_account_id: int | None = None
    subscription_id: int | None = None
    internal_note: str | None = None

    @field_validator("amount", "percentage", mode="before")
    @classmethod
    def reject_float_decimals(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError("Decimal values must be supplied as strings.")
        return value

    @field_validator(
        "customer_id",
        "recharge_partner_id",
        "customer_invoice_id",
        "target_company_id",
        "opportunity_id",
        "sales_order_id",
        "sales_order_line_id",
        "proposal_scenario_id",
        "purchase_order_id",
        "project_id",
        "analytic_account_id",
        "subscription_id",
        mode="before",
    )
    @classmethod
    def reject_boolean_ids(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("ERP identifiers must be integers.")
        return value


class BusinessContextAllocationSetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allocations: list[BusinessContextAllocationRequest]
    completeness: AllocationCompleteness = AllocationCompleteness.COMPLETE
    invoice_total: Decimal | None = None
    currency: str | None = None

    @field_validator("invoice_total", mode="before")
    @classmethod
    def reject_float_invoice_total(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError("Decimal values must be supplied as strings.")
        return value


class ReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True, use_enum_values=False)

    expected_version: int
    decision: ReviewDecisionType
    selected_workflow: WorkflowType | None = None
    selected_partner_id: int | None = None
    line_resolutions: list[LineResolutionRequest] = Field(default_factory=list)
    tax_resolutions: list[TaxResolutionRequest] = Field(default_factory=list)
    business_context_allocations: BusinessContextAllocationSetRequest | None = None
    selected_quotation_scenario_ids: list[str] = Field(default_factory=list)
    comment: str | None = None
    idempotency_key: str


class ReviewDecisionAcknowledgementResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    accepted: bool
    review_id: str
    status: ReviewStatus
    version: int
    decision: ReviewDecisionType
    selected_workflow: WorkflowType | None = None


class SupplierResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    mode: SupplierResolutionMode
    expected_version: int
    partner_id: int | None = None
    note: str | None = None
    authorization_id: str | None = Field(default=None, min_length=1, max_length=36)


class SupplierRemediationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    review_id: str
    company_id: int
    mode: SupplierResolutionMode
    resolution_status: SupplierRemediationStatus
    previous_version: int
    current_version: int
    current_workflow: WorkflowType
    current_review_reasons: list[ManualReviewReasonResponse]
    partner_id: int | None = None
    partner_write_status: SupplierPartnerWriteEffectStatus | None = None
    reclassified: bool
    already_applied: bool
    workbench_republished: bool
    #: ``True`` only for ``mode=one_off_vendor``: the effective partner is Hub-owned via
    #: ONE_OFF_VENDOR. ``None`` for every other mode.
    one_off_vendor_hub_owned: bool | None = None
    #: The archive-last lifecycle state for this review's ONE_OFF_VENDOR retirement, if any.
    #: ``None`` for every other mode, or if no retirement row could be read.
    one_off_vendor_retirement_status: OneOffVendorRetirementStatus | None = None
    #: Derived convenience flags for an operator/UI -- both ``None`` unless
    #: ``one_off_vendor_retirement_status`` is set.
    one_off_vendor_awaiting_vendor_bill: bool | None = None
    one_off_vendor_reconciliation_required: bool | None = None
    safe_message: str | None = None


SupplierRemediationEnvelope = ApiEnvelope[SupplierRemediationResponse]


class ProductResolutionMode(StrEnum):
    """Explicit operator choices for resolving a review line whose product is not matched.

    A policy enum mirroring ``SupplierResolutionMode`` -- not an Odoo/HTTP implementation
    detail. Exactly one member today; the shape leaves room for a future mode (e.g.
    matching an existing product) without a breaking request schema change.
    """

    CREATE_NEW_PRODUCT = "create_new_product"


class ProductResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: ProductResolutionMode
    expected_version: int
    line_number: str
    product_name: str
    # No default: the operator must explicitly choose goods vs. service. Never inferred
    # from product_name, seller_item_code, supplier, invoice description, uom_id, or
    # historical matching (P0-PROD-07F product-standard audit finding).
    product_type: Literal["consu", "service"]
    uom_id: int
    internal_reference: str | None = None
    is_storable: bool = False
    note: str | None = None
    authorization_id: str | None = Field(default=None, min_length=1, max_length=36)


class ProductRemediationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    review_id: str
    company_id: int
    review_version: int
    line_number: str
    resolution_status: ProductRemediationStatus
    product_template_id: int | None = None
    product_id: int | None = None
    supplierinfo_id: int | None = None
    created_product: bool
    created_supplierinfo: bool
    reused_existing_product: bool
    already_applied: bool
    needs_reconciliation: bool
    safe_message: str | None = None


ProductRemediationEnvelope = ApiEnvelope[ProductRemediationResponse]


class ExpenseAccountCandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    code: str
    name: str
    account_type: str


ExpenseAccountCandidatesEnvelope = ApiEnvelope[list[ExpenseAccountCandidateResponse]]


class OperatingExpenseMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_version: int
    expense_account_id: int
    expense_category: str
    note: str | None = None


class OperatingExpenseMappingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    review_id: str
    company_id: int
    resolution_status: OperatingExpenseMappingSubmissionStatus
    previous_version: int
    current_version: int
    current_workflow: WorkflowType
    current_review_reasons: list[ManualReviewReasonResponse]
    vendor_partner_id: int
    expense_account_id: int
    expense_category: str
    mapping_outcome: OperatingExpenseMappingOnboardingOutcome
    reclassified: bool
    already_applied: bool
    safe_message: str | None = None


OperatingExpenseMappingEnvelope = ApiEnvelope[OperatingExpenseMappingResponse]


class PurchasePurposeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_version: int
    purchase_purpose: PurchasePurpose
    note: str | None = None


class PurchasePurposeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    review_id: str
    company_id: int
    review_version: int
    purchase_purpose: PurchasePurpose
    already_applied: bool
    safe_message: str | None = None


PurchasePurposeEnvelope = ApiEnvelope[PurchasePurposeResponse]


class AccountingResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_version: int
    #: Deliberately a fixed ``Literal``, not the broader policy enum: any other value
    #: (e.g. a future "capitalize_fixed_asset") must be rejected by request validation
    #: itself, never silently accepted and reinterpreted (P0-PROD-15T scope).
    treatment_type: Literal["expense_account"]
    expense_account_id: int
    expense_category: str
    note: str | None = None


class AccountingResolutionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    review_id: str
    company_id: int
    resolution_status: AccountingResolutionStatus
    previous_version: int
    current_version: int
    current_workflow: WorkflowType
    current_review_reasons: list[ManualReviewReasonResponse]
    treatment_type: AccountingTreatmentType
    expense_account_id: int
    expense_category: str
    reclassified: bool
    already_applied: bool
    safe_message: str | None = None


AccountingResolutionEnvelope = ApiEnvelope[AccountingResolutionResponse]


class ExecutionEvidenceRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_version: int


class ExecutionEvidenceRecoveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str
    company_id: int
    review_version: int
    already_applied: bool
    partner_id: int | None = None
    expense_account_id: int | None = None
    expense_category: str | None = None
    safe_message: str | None = None


ExecutionEvidenceRecoveryEnvelope = ApiEnvelope[ExecutionEvidenceRecoveryResponse]


class WorkbenchDecisionIngestionCandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    review_id: str | None
    odoo_record_id: int | None
    status: WorkbenchDecisionIngestionStatus
    acknowledged: bool
    idempotency_key: str | None = None
    message: str | None = None


class WorkbenchDecisionIngestionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    company_id: int
    processed_count: int
    already_processed_count: int
    acknowledgement_failed_count: int
    failed_count: int
    results: list[WorkbenchDecisionIngestionCandidateResponse]


class ExecutionApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approved_by: str


class WorkbenchVendorBillExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_version: int
    mode: ExecutionMode = ExecutionMode.DRY_RUN
    approval: ExecutionApprovalRequest | None = None
    authorization_id: str | None = Field(default=None, min_length=1, max_length=36)


class ExecutionArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    artifact_type: ExecutionArtifactType
    artifact_id: str
    external_identity: str
    created: bool


class WorkbenchVendorBillExecutionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    review_id: str
    company_id: int
    decision_version: int
    mode: ExecutionMode
    status: WorkbenchVendorBillExecutionStatus
    execution_id: str | None = None
    runtime_state: str | None = None
    artifacts: list[ExecutionArtifactResponse]
    message: str | None = None


class VendorBillPreviewLineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    line_number: str | None
    description: str | None
    quantity: str
    unit_price: str
    account_id: int | None
    product_id: int | None
    tax_ids: list[int]


class VendorBillPreviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    review_id: str
    company_id: int
    decision_version: int
    decision_id: str | None
    selected_workflow: WorkflowType

    move_type: str
    partner_id: int
    invoice_date: date
    reference: str | None
    header_company_id: int | None
    currency_code: str
    currency_id: int

    idempotency_key: str

    lines: list[VendorBillPreviewLineResponse]

    gross_source_amount: str
    total_discount: str
    preview_untaxed: str
    preview_tax: str
    preview_total: str


class VendorBillReadbackLineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    line_id: int
    account_id: int | None
    product_id: int | None
    quantity: str
    price_unit: str
    tax_ids: list[int]
    price_subtotal: str
    price_total: str


class VendorBillReadbackResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str
    execution_id: str
    artifact_id: str
    move_id: int
    state: str
    move_type: str
    partner_id: int
    currency: str
    amount_untaxed: str
    amount_tax: str
    amount_total: str
    lines: list[VendorBillReadbackLineResponse]


class WorkbenchQuotationScenarioEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_version: int


class WorkbenchQuotationScenarioEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    review_id: str
    company_id: int
    decision_version: int
    status: WorkbenchQuotationScenarioEvidenceStatus
    decision_id: str | None = None
    persisted_scenario_ids: list[str]
    message: str | None = None


ReviewItemEnvelope = ApiEnvelope[ReviewItemResponse]
ReviewQueueEnvelope = ApiEnvelope[ReviewQueueResponse]
ReviewDecisionAcknowledgementEnvelope = ApiEnvelope[ReviewDecisionAcknowledgementResponse]
WorkbenchDecisionIngestionEnvelope = ApiEnvelope[WorkbenchDecisionIngestionResponse]
WorkbenchVendorBillExecutionEnvelope = ApiEnvelope[WorkbenchVendorBillExecutionResponse]
VendorBillPreviewEnvelope = ApiEnvelope[VendorBillPreviewResponse]
VendorBillReadbackEnvelope = ApiEnvelope[VendorBillReadbackResponse]
WorkbenchQuotationScenarioEvidenceEnvelope = ApiEnvelope[WorkbenchQuotationScenarioEvidenceResponse]
ErrorEnvelope = ApiEnvelope[Any]


def decimal_to_api(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


class WriteAuthorizationIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_version: int = Field(gt=0)
    operation_type: WriteAuthorizationOperationType = WriteAuthorizationOperationType.EXECUTE_VENDOR_BILL
    justification: str | None = Field(default=None, max_length=2000)


class WriteAuthorizationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    authorization_id: str
    company_id: int
    review_id: str
    operation_type: WriteAuthorizationOperationType
    target_version: int
    status: WriteAuthorizationStatus
    authorized_by: str
    created_at: datetime
    expires_at: datetime
    is_expired: bool
    justification: str | None
    consumed_at: datetime | None
    consumed_by_trace_id: str | None
    consumed_by_execution_id: str | None
    revoked_at: datetime | None
    revoked_by: str | None
    use_count: int
    last_used_at: datetime | None
    last_used_trace_id: str | None


WriteAuthorizationEnvelope = ApiEnvelope[WriteAuthorizationResponse]
WriteAuthorizationsEnvelope = ApiEnvelope[list[WriteAuthorizationResponse]]


class OneOffVendorRetirementResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)
    review_id: str
    company_id: int
    review_version: int
    resolved_partner_id: int
    status: OneOffVendorRetirementStatus


class OneOffVendorRetirementRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    review_version: int = Field(gt=0)
    authorization_id: str | None = Field(default=None, min_length=1, max_length=36)


class OneOffVendorRetirementRecoveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)
    review_id: str
    company_id: int
    review_version: int
    resolved_partner_id: int | None
    status: ArchiveOneOffVendorStatus
    already_applied: bool
    safe_message: str | None


OneOffVendorRetirementEnvelope = ApiEnvelope[OneOffVendorRetirementResponse]
OneOffVendorRetirementRecoveryEnvelope = ApiEnvelope[OneOffVendorRetirementRecoveryResponse]


class WorkbenchExecutionDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    decision_id: str | None
    decision_version: int
    selected_workflow: WorkflowType | None


class WorkbenchExecutionSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    execution_id: str
    mode: ExecutionMode
    state: ExecutionState
    retry_count: int
    max_attempts: int
    remaining_attempts: int
    retry_possible: bool


class WorkbenchExecutionFailureResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_key: str | None
    error_code: str
    safe_message: str


class WorkbenchEvidenceStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage_one_present: bool
    stage_one_review_version: int | None
    stage_two_present: bool
    stage_two_decision_version: int | None


class WorkbenchExecutionAuthorizationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    authorization_id: str
    operation_type: WriteAuthorizationOperationType
    target_version: int
    status: WriteAuthorizationStatus
    use_count: int
    is_expired: bool
    consumed_by_execution_id: str | None


class WorkbenchRecoveryStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_completed: bool
    waiting_retry: bool
    remaining_attempts: int


class WorkbenchExecutionStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    review_id: str
    company_id: int
    review_version: int
    review_status: ReviewStatus

    decision: WorkbenchExecutionDecisionResponse | None
    execution: WorkbenchExecutionSummaryResponse | None
    failure: WorkbenchExecutionFailureResponse | None
    artifacts: list[ExecutionArtifactResponse]
    evidence: WorkbenchEvidenceStatusResponse
    authorization: WorkbenchExecutionAuthorizationResponse | None
    recovery: WorkbenchRecoveryStatusResponse


WorkbenchExecutionStatusEnvelope = ApiEnvelope[WorkbenchExecutionStatusResponse]
