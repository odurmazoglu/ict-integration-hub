from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.application.workbench.allocations import AllocationCompleteness, BusinessContextAllocationType
from app.application.workbench.decision_ingestion import WorkbenchDecisionIngestionStatus
from app.application.workbench.dto import ReviewDecisionType, ReviewStatus
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


class ReviewQueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[ReviewItemResponse]
    total_count: int
    limit: int
    offset: int


class LineResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    line_number: str
    selected_product_id: int


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


ReviewItemEnvelope = ApiEnvelope[ReviewItemResponse]
ReviewQueueEnvelope = ApiEnvelope[ReviewQueueResponse]
ReviewDecisionAcknowledgementEnvelope = ApiEnvelope[ReviewDecisionAcknowledgementResponse]
WorkbenchDecisionIngestionEnvelope = ApiEnvelope[WorkbenchDecisionIngestionResponse]
ErrorEnvelope = ApiEnvelope[Any]


def decimal_to_api(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")
