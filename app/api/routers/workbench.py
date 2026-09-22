from __future__ import annotations

from datetime import datetime
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer

from app.api.dependencies import (
    CreateNewProductUseCaseDep,
    CreateWriteAuthorizationUseCaseDep,
    GetReviewItemUseCaseDep,
    ListReviewQueueUseCaseDep,
    ListWriteAuthorizationsUseCaseDep,
    OneOffVendorRetirementUseCaseDep,
    RecoverOneOffVendorRetirementWorkflowDep,
    RequestContextDep,
    ResolveWorkbenchSupplierUseCaseDep,
    RevokeWriteAuthorizationUseCaseDep,
    SubmitReviewDecisionUseCaseDep,
    VendorBillPreviewUseCaseDep,
    WorkbenchAcceptedDecisionExecutionDispatcherDep,
    WorkbenchDecisionIngestionWorkflowDep,
    WorkbenchExecutionStatusUseCaseDep,
    WorkbenchQuotationScenarioEvidenceWorkflowDep,
)
from app.api.error_handling import error_response_factory
from app.api.security import Permission, PermissionDeniedError, require_permission
from app.application.exceptions.product_remediation import (
    ProductWriteError,
    ProductWriteSafetyGateError,
    SupplierInfoWriteError,
)
from app.application.exceptions.supplier_partner import (
    SupplierPartnerWriteError,
    SupplierPartnerWriteSafetyGateError,
)
from app.application.execution import (
    ExecutionApproval,
    ExecutionArtifact,
    ExecutionPlanningError,
    WorkbenchVendorBillExecutionResult,
)
from app.application.execution.exceptions import (
    ExecutionPreviewCurrencyResolutionError,
    ExecutionPreviewUnsupportedWorkflowError,
    ExecutionSourceInvoiceError,
    ExecutionSourceInvoiceIntegrityError,
    ExecutionSourceInvoiceNotFoundError,
)
from app.application.execution.vendor_bill_preview import PreviewVendorBillRequest, VendorBillPreview
from app.application.quotation import WorkbenchQuotationScenarioEvidenceResult
from app.application.workbench import (
    BusinessContextAllocation,
    BusinessContextAllocationSet,
    LineResolution,
    ReviewDecisionAcknowledgement,
    ReviewDecisionCommand,
    ReviewDetailQuery,
    ReviewItem,
    ReviewQueueQuery,
    ReviewQueueResult,
    ReviewStatus,
    TaxResolution,
    WorkbenchDecisionIngestionResult,
)
from app.application.workbench.exceptions import (
    ProductRemediationConflictError,
    ProductRemediationContractError,
    ProductRemediationDataIntegrityError,
    ProductRemediationEligibilityError,
    ProductRemediationIdentityAmbiguousError,
    ProductRemediationRaceError,
    ProductRemediationSupplierUnresolvedError,
    ReviewDataIntegrityError,
    ReviewDecisionDataIntegrityError,
    ReviewDecisionError,
    ReviewDecisionIdempotencyConflictError,
    ReviewNotFoundError,
    ReviewPersistenceError,
    ReviewQueryError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
    SupplierResolutionConflictError,
    SupplierResolutionContractError,
    SupplierResolutionDataIntegrityError,
    SupplierResolutionError,
    SupplierResolutionOneOffVendorNotHubOwnedError,
    SupplierResolutionPartnerInactiveError,
    SupplierResolutionPartnerMismatchError,
    SupplierResolutionPartnerNotFoundError,
    SupplierResolutionRaceError,
    WorkbenchContractError,
)
from app.application.workbench.execution_status import WorkbenchExecutionStatus
from app.application.workbench.one_off_vendor_retirement import ArchiveOneOffVendorCommand, OneOffVendorRetirementStatus
from app.application.workbench.product_remediation import CreateNewProductCommand, ProductRemediationStatus
from app.application.workbench.supplier_remediation import ResolveWorkbenchSupplierCommand
from app.application.workbench.write_authorization import (
    WriteAuthorizationAlreadyConsumedError,
    WriteAuthorizationError,
    WriteAuthorizationExpiredError,
    WriteAuthorizationNotFoundError,
    WriteAuthorizationRevokedError,
    WriteAuthorizationScopeMismatchError,
)
from app.application.workflow import ManualReviewReason, WorkflowType
from app.billing.exceptions import VendorBillBuildError
from app.schemas.workbench import (
    ApiEnvelope,
    BusinessContextAllocationRequest,
    BusinessContextAllocationSetRequest,
    ExecutionApprovalRequest,
    ExecutionArtifactResponse,
    LineResolutionRequest,
    ManualReviewReasonResponse,
    OneOffVendorRetirementEnvelope,
    OneOffVendorRetirementRecoveryEnvelope,
    OneOffVendorRetirementRecoveryRequest,
    OneOffVendorRetirementRecoveryResponse,
    OneOffVendorRetirementResponse,
    ProductRemediationEnvelope,
    ProductRemediationResponse,
    ProductResolutionRequest,
    ReviewDecisionAcknowledgementEnvelope,
    ReviewDecisionAcknowledgementResponse,
    ReviewDecisionRequest,
    ReviewItemEnvelope,
    ReviewItemResponse,
    ReviewQueueEnvelope,
    ReviewQueueResponse,
    SupplierRemediationEnvelope,
    SupplierRemediationResponse,
    SupplierResolutionRequest,
    TaxResolutionRequest,
    VendorBillPreviewEnvelope,
    VendorBillPreviewLineResponse,
    VendorBillPreviewResponse,
    WorkbenchDecisionIngestionCandidateResponse,
    WorkbenchDecisionIngestionEnvelope,
    WorkbenchDecisionIngestionResponse,
    WorkbenchEvidenceStatusResponse,
    WorkbenchExecutionAuthorizationResponse,
    WorkbenchExecutionDecisionResponse,
    WorkbenchExecutionFailureResponse,
    WorkbenchExecutionStatusEnvelope,
    WorkbenchExecutionStatusResponse,
    WorkbenchExecutionSummaryResponse,
    WorkbenchQuotationScenarioEvidenceEnvelope,
    WorkbenchQuotationScenarioEvidenceRequest,
    WorkbenchQuotationScenarioEvidenceResponse,
    WorkbenchRecoveryStatusResponse,
    WorkbenchVendorBillExecutionEnvelope,
    WorkbenchVendorBillExecutionRequest,
    WorkbenchVendorBillExecutionResponse,
    WriteAuthorizationEnvelope,
    WriteAuthorizationIssueRequest,
    WriteAuthorizationResponse,
    WriteAuthorizationsEnvelope,
    decimal_to_api,
)

_bearer_scheme = HTTPBearer(auto_error=False)
router = APIRouter(prefix="/api/workbench", tags=["workbench"], dependencies=[Depends(_bearer_scheme)])

QUEUE_QUERY_PARAMS = frozenset(
    {
        "status",
        "limit",
        "offset",
        "created_from",
        "created_to",
        "supplier_tax_number",
        "workflow",
    }
)
COMMON_ERROR_RESPONSES = {
    400: {"model": ApiEnvelope[object], "description": "Invalid Workbench request."},
    401: {"model": ApiEnvelope[object], "description": "Authentication is required or invalid."},
    403: {"model": ApiEnvelope[object], "description": "Required permission is missing."},
    404: {"model": ApiEnvelope[object], "description": "Review item was not found."},
    409: {"model": ApiEnvelope[object], "description": "Review state, version, or idempotency conflict."},
    500: {"model": ApiEnvelope[object], "description": "Safe Workbench persistence or query failure."},
    503: {"model": ApiEnvelope[object], "description": "Authentication provider is unavailable."},
}


@router.get(
    "/reviews",
    response_model=ReviewQueueEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="List Import Workbench reviews",
    description="Requires workbench_review_read. Company identity comes from the trusted RequestContext.",
)
def list_review_queue(
    request: Request,
    response: Response,
    context: RequestContextDep,
    use_case: ListReviewQueueUseCaseDep,
    status: Annotated[ReviewStatus, Query()] = ReviewStatus.PENDING_REVIEW,
    limit: Annotated[int, Query()] = 50,
    offset: Annotated[int, Query()] = 0,
    created_from: Annotated[datetime | None, Query()] = None,
    created_to: Annotated[datetime | None, Query()] = None,
    supplier_tax_number: Annotated[str | None, Query()] = None,
    workflow: Annotated[WorkflowType | None, Query()] = None,
) -> ReviewQueueEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_REVIEW_READ)(context)
        _reject_unsupported_query_params(request, QUEUE_QUERY_PARAMS)
        result = use_case.execute(
            ReviewQueueQuery(
                company_id=context.company_id,
                status=status,
                limit=limit,
                offset=offset,
                created_from=created_from,
                created_to=created_to,
                supplier_tax_number=supplier_tax_number,
                workflow=workflow,
            )
        )
        return _success(response, context.trace_id, _queue_response(result), warnings=[])
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


@router.get(
    "/reviews/{review_id}",
    response_model=ReviewItemEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Get Import Workbench review detail",
    description="Requires workbench_review_read. Detail reads are scoped by review_id and RequestContext.company_id.",
)
def get_review_detail(
    review_id: str,
    response: Response,
    context: RequestContextDep,
    use_case: GetReviewItemUseCaseDep,
) -> ReviewItemEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_REVIEW_READ)(context)
        item = use_case.execute(ReviewDetailQuery(review_id=review_id, company_id=context.company_id))
        return _success(response, context.trace_id, _review_item_response(item), warnings=list(item.warnings))
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


@router.post(
    "/decisions/sync",
    response_model=WorkbenchDecisionIngestionEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Ingest ready Odoo Workbench decisions",
    description=(
        "Requires workbench_review_decide. Reads Odoo Workbench rows marked ready for Hub processing, persists "
        "canonical Hub decision evidence, then acknowledges Odoo. It does not execute workflows or create ERP records."
    ),
)
def sync_odoo_workbench_decisions(
    response: Response,
    context: RequestContextDep,
    workflow: WorkbenchDecisionIngestionWorkflowDep,
    limit: Annotated[int, Query()] = 50,
) -> WorkbenchDecisionIngestionEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_REVIEW_DECIDE)(context)
        result = workflow.sync_ready_decisions(company_id=context.company_id, limit=limit, trace_id=context.trace_id)
        return _success(response, context.trace_id, _decision_ingestion_response(result), warnings=[])
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


@router.post(
    "/reviews/{review_id}/execute",
    response_model=WorkbenchVendorBillExecutionEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Execute an accepted Workbench decision",
    description=(
        "Requires workbench_execute. Executes an already persisted canonical accepted decision using pinned Hub "
        "evidence, routed by the decision's selected workflow. Vendor Bill decisions run the Vendor Bill flow; "
        "CUSTOMER_QUOTATION decisions create one draft sale.order per selected scenario from immutable quotation "
        "evidence (capture it first via the quotation-scenarios endpoint). The request cannot provide ERP payloads."
    ),
)
def execute_workbench_vendor_bill(
    review_id: str,
    request_body: WorkbenchVendorBillExecutionRequest,
    response: Response,
    context: RequestContextDep,
    workflow: WorkbenchAcceptedDecisionExecutionDispatcherDep,
) -> WorkbenchVendorBillExecutionEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_EXECUTE)(context)
        if request_body.authorization_id is not None:
            result = workflow.execute(
                review_id=review_id,
                company_id=context.company_id,
                decision_version=request_body.decision_version,
                mode=request_body.mode,
                approval=_execution_approval(request_body.approval),
                trace_id=context.trace_id,
                authorization_id=request_body.authorization_id,
            )
        else:
            result = workflow.execute(
                review_id=review_id,
                company_id=context.company_id,
                decision_version=request_body.decision_version,
                mode=request_body.mode,
                approval=_execution_approval(request_body.approval),
                trace_id=context.trace_id,
            )
        return _success(response, context.trace_id, _vendor_bill_execution_response(result), warnings=[])
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


@router.get(
    "/reviews/{review_id}/vendor-bill-preview",
    response_model=VendorBillPreviewEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Preview the Vendor Bill an accepted decision would produce",
    description=(
        "Requires workbench_execute -- the same permission that already governs visibility into execution "
        "status/artifacts; no write permission exists or is required for this read-only operation. Computes the "
        "exact Vendor Bill economics and routing EXECUTE would produce for the given decision_version, using "
        "only the persisted accepted decision, persisted Stage-2 execution evidence, and the same VendorBillBuilder "
        "real execution uses -- it performs no current-time partner/product/tax matching and no account/product "
        "selection of its own. Makes exactly one read-only Odoo call (res.currency resolution, identical to what "
        "EXECUTE's writer already performs) and zero Odoo or Hub writes. Works regardless of "
        "EXECUTION_EXECUTE_ENABLED or any other business-write gate. preview_untaxed/preview_tax/preview_total are "
        "deterministic PREVIEW totals computed before any Odoo Vendor Bill record exists -- never Odoo-computed "
        "values. "
        "idempotency_key is the exact identity a later EXECUTE call would use for this Vendor Bill step."
    ),
)
def preview_workbench_vendor_bill(
    review_id: str,
    decision_version: Annotated[int, Query()],
    response: Response,
    context: RequestContextDep,
    use_case: VendorBillPreviewUseCaseDep,
) -> VendorBillPreviewEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_EXECUTE)(context)
        preview = use_case.preview(
            PreviewVendorBillRequest(
                review_id=review_id,
                company_id=context.company_id,
                decision_version=decision_version,
            )
        )
        return _success(response, context.trace_id, _vendor_bill_preview_response(preview), warnings=[])
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


@router.get(
    "/reviews/{review_id}/execution-status",
    response_model=WorkbenchExecutionStatusEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Read operator execution/recovery status for a Workbench review (P0-PROD-12A)",
    description=(
        "Requires workbench_execute. Read-only composition of already-persisted execution/retry/artifact/"
        "evidence/authorization state for the review's latest accepted decision and its most recent real "
        "(EXECUTE-mode) accepted-decision execution, if any. Performs zero writes and never calls Odoo. Makes "
        "the P0-PROD-10C/10G incident shape (waiting_retry, retry_count, prior failure, created Vendor Bill "
        "artifact id) diagnosable without SSH/psql/internal Python."
    ),
)
def get_workbench_execution_status(
    review_id: str,
    response: Response,
    context: RequestContextDep,
    use_case: WorkbenchExecutionStatusUseCaseDep,
) -> WorkbenchExecutionStatusEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_EXECUTE)(context)
        status = use_case.execute(review_id=review_id, company_id=context.company_id)
        return _success(response, context.trace_id, _workbench_execution_status_response(status), warnings=[])
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


@router.post(
    "/reviews/{review_id}/quotation-scenarios",
    response_model=WorkbenchQuotationScenarioEvidenceEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Capture accepted customer quotation scenario evidence",
    description=(
        "Requires workbench_execute. Post-acceptance step for an already persisted CUSTOMER_QUOTATION decision: "
        "reads the durable accepted decision, captures each frozen selected scenario from Odoo read-only, and "
        "persists immutable Hub quotation scenario evidence. Idempotent on retry; performs no sale.order write."
    ),
)
def capture_workbench_quotation_scenarios(
    review_id: str,
    request_body: WorkbenchQuotationScenarioEvidenceRequest,
    response: Response,
    context: RequestContextDep,
    workflow: WorkbenchQuotationScenarioEvidenceWorkflowDep,
) -> WorkbenchQuotationScenarioEvidenceEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_EXECUTE)(context)
        result = workflow.capture(
            review_id=review_id,
            company_id=context.company_id,
            decision_version=request_body.decision_version,
            trace_id=context.trace_id,
        )
        return _success(
            response,
            context.trace_id,
            _quotation_scenario_evidence_response(result),
            warnings=[],
        )
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


@router.post(
    "/reviews/{review_id}/decision",
    response_model=ReviewDecisionAcknowledgementEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Submit Import Workbench review decision",
    description=(
        "Requires workbench_review_decide. Persists explicit user intent only; it does not execute workflows "
        "or create ERP records."
    ),
)
def submit_review_decision(
    review_id: str,
    request_body: ReviewDecisionRequest,
    response: Response,
    context: RequestContextDep,
    use_case: SubmitReviewDecisionUseCaseDep,
) -> ReviewDecisionAcknowledgementEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_REVIEW_DECIDE)(context)
        _reject_extra_decision_fields(request_body)
        acknowledgement = use_case.execute(
            _decision_command(review_id, context.company_id, context.user_id, request_body)
        )
        return _success(
            response,
            context.trace_id,
            _acknowledgement_response(acknowledgement),
            warnings=list(acknowledgement.warnings),
        )
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


@router.post(
    "/reviews/{review_id}/supplier-resolution",
    response_model=SupplierRemediationEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Resolve a missing supplier for an Import Workbench review",
    description=(
        "Requires workbench_review_decide. For a review whose reasons include SUPPLIER_NOT_FOUND, records an "
        "explicit resolution -- MATCH_EXISTING (select an existing partner), CREATE_PERMANENT_SUPPLIER (create a "
        "normal ongoing ICT supplier), ONE_OFF_VENDOR (create/reuse a Hub-owned partner for a single one-off "
        "purchase, retired once a Vendor Bill durably succeeds -- see one_off_vendor_retirement_status in the "
        "response), or USE_ONE_OFF_SUPPLIER (record intent only; deferred) -- and triggers the non-destructive "
        "SUPPLIER_RESOLUTION reclassification. Legal supplier identity (name, VAT) always comes only from the "
        "review's immutable source evidence -- never the request body; the request only selects the mode. "
        "CREATE_PERMANENT_SUPPLIER and ONE_OFF_VENDOR are both gated by SUPPLIER_REMEDIATION_WRITE_ENABLED, or by "
        "an optional authorization_id from a pre-issued narrow write authorization scoped to exactly this "
        "review/version/operation (see the write-authorizations endpoint) -- either way the master production "
        "kill switch and named-approver checks remain absolute. This endpoint never executes a Vendor Bill and "
        "never archives a partner."
    ),
)
async def resolve_review_supplier(
    review_id: str,
    request_body: SupplierResolutionRequest,
    response: Response,
    context: RequestContextDep,
    use_case: ResolveWorkbenchSupplierUseCaseDep,
) -> SupplierRemediationEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_REVIEW_DECIDE)(context)
        result = await use_case.execute(
            ResolveWorkbenchSupplierCommand(
                review_id=review_id,
                company_id=context.company_id,
                expected_version=request_body.expected_version,
                mode=request_body.mode,
                approved_by=context.user_name or context.user_id,
                resolved_partner_id=request_body.partner_id,
                note=request_body.note,
                authorization_id=request_body.authorization_id,
            )
        )
        return _success(
            response,
            context.trace_id,
            _supplier_remediation_response(result),
            warnings=[],
        )
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


@router.post(
    "/reviews/{review_id}/product-resolution",
    response_model=ProductRemediationEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Create/remediate an Odoo product for a missing-product Import Workbench review line",
    description=(
        "Requires workbench_review_decide. For one review line whose reasons include PRODUCT_NOT_FOUND, delegates "
        "to the crash-safe CREATE_NEW_PRODUCT orchestration to create a new Odoo product.template/supplierinfo, or "
        "safely reuse/reconcile an existing one. Requires the review's supplier to already be resolved (see the "
        "supplier-resolution endpoint) -- this endpoint never creates or resolves a supplier itself. Gated by "
        "PRODUCT_REMEDIATION_WRITE_ENABLED, or by a narrow single-use CREATE_NEW_PRODUCT write authorization "
        "(authorization_id) issued via the write-authorizations endpoint for this exact review/version -- either "
        "way the production master kill switch and named-approver requirement are never bypassed. It never "
        "submits a review decision, pins selected_product_id, or executes a Vendor Bill -- those remain separate "
        "explicit operator actions."
    ),
)
async def resolve_review_product(
    review_id: str,
    request_body: ProductResolutionRequest,
    response: Response,
    context: RequestContextDep,
    use_case: CreateNewProductUseCaseDep,
) -> ProductRemediationEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_REVIEW_DECIDE)(context)
        result = await use_case.execute(
            CreateNewProductCommand(
                review_id=review_id,
                company_id=context.company_id,
                expected_version=request_body.expected_version,
                line_number=request_body.line_number,
                product_name=request_body.product_name,
                product_type=request_body.product_type,
                uom_id=request_body.uom_id,
                approved_by=context.user_name or context.user_id,
                is_storable=request_body.is_storable,
                internal_reference=request_body.internal_reference,
                note=request_body.note,
                authorization_id=request_body.authorization_id,
            )
        )
        return _success(
            response,
            context.trace_id,
            _product_remediation_response(result),
            warnings=[],
        )
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


def _product_remediation_response(result) -> ProductRemediationResponse:
    return ProductRemediationResponse(
        review_id=result.review_id,
        company_id=result.company_id,
        review_version=result.review_version,
        line_number=result.line_number,
        resolution_status=result.status,
        product_template_id=result.product_template_id,
        product_id=result.product_id,
        supplierinfo_id=result.supplierinfo_id,
        created_product=result.created_product,
        created_supplierinfo=result.created_supplierinfo,
        reused_existing_product=result.reused_existing_product,
        already_applied=result.already_applied,
        needs_reconciliation=result.status is ProductRemediationStatus.RECONCILIATION_REQUIRED,
        safe_message=result.safe_message,
    )


def _supplier_remediation_response(result) -> SupplierRemediationResponse:
    retirement_status = result.one_off_vendor_retirement_status
    return SupplierRemediationResponse(
        review_id=result.review_id,
        company_id=result.company_id,
        mode=result.mode,
        resolution_status=result.status,
        previous_version=result.previous_version,
        current_version=result.current_version,
        current_workflow=result.current_workflow,
        current_review_reasons=[_reason_response(reason) for reason in result.current_review_reasons],
        partner_id=result.effective_partner_id,
        partner_write_status=result.partner_write_status,
        reclassified=result.reclassified,
        already_applied=result.already_applied,
        workbench_republished=result.workbench_republished,
        one_off_vendor_hub_owned=result.one_off_vendor_hub_owned,
        one_off_vendor_retirement_status=retirement_status,
        one_off_vendor_awaiting_vendor_bill=(
            retirement_status is OneOffVendorRetirementStatus.PENDING_VENDOR_BILL
            if retirement_status is not None
            else None
        ),
        one_off_vendor_reconciliation_required=(
            retirement_status is OneOffVendorRetirementStatus.NEEDS_RECONCILIATION
            if retirement_status is not None
            else None
        ),
        safe_message=result.safe_message,
    )


def _decision_command(
    review_id: str,
    company_id: int,
    decided_by: str,
    request: ReviewDecisionRequest,
) -> ReviewDecisionCommand:
    return ReviewDecisionCommand(
        review_id=review_id,
        company_id=company_id,
        expected_version=request.expected_version,
        decision=request.decision,
        decided_by=decided_by,
        idempotency_key=request.idempotency_key,
        selected_workflow=request.selected_workflow,
        selected_partner_id=request.selected_partner_id,
        line_resolutions=tuple(_line_resolution(value) for value in request.line_resolutions),
        tax_resolutions=tuple(_tax_resolution(value) for value in request.tax_resolutions),
        business_context_allocations=_business_context_allocations(request.business_context_allocations),
        selected_quotation_scenario_ids=tuple(request.selected_quotation_scenario_ids),
        comment=request.comment,
    )


def _line_resolution(value: LineResolutionRequest) -> LineResolution:
    return LineResolution(
        line_number=value.line_number,
        selected_product_id=value.selected_product_id,
        account_only=value.account_only,
        expense_account_id=value.expense_account_id,
    )


def _tax_resolution(value: TaxResolutionRequest) -> TaxResolution:
    return TaxResolution(
        line_number=value.line_number,
        tax_index=value.tax_index,
        selected_tax_id=value.selected_tax_id,
    )


def _business_context_allocations(
    value: BusinessContextAllocationSetRequest | None,
) -> BusinessContextAllocationSet | None:
    if value is None:
        return None
    return BusinessContextAllocationSet(
        allocations=tuple(_business_context_allocation(allocation) for allocation in value.allocations),
        completeness=value.completeness,
        invoice_total=value.invoice_total,
        currency=value.currency,
    )


def _business_context_allocation(value: BusinessContextAllocationRequest) -> BusinessContextAllocation:
    return BusinessContextAllocation(
        allocation_key=value.allocation_key,
        allocation_type=value.allocation_type,
        source_line_number=value.source_line_number,
        description=value.description,
        amount=value.amount,
        percentage=value.percentage,
        currency=value.currency,
        customer_id=value.customer_id,
        recharge_partner_id=value.recharge_partner_id,
        customer_invoice_id=value.customer_invoice_id,
        target_company_id=value.target_company_id,
        opportunity_id=value.opportunity_id,
        sales_order_id=value.sales_order_id,
        sales_order_line_id=value.sales_order_line_id,
        proposal_scenario_id=value.proposal_scenario_id,
        purchase_order_id=value.purchase_order_id,
        project_id=value.project_id,
        analytic_account_id=value.analytic_account_id,
        subscription_id=value.subscription_id,
        internal_note=value.internal_note,
    )


def _queue_response(result: ReviewQueueResult) -> ReviewQueueResponse:
    return ReviewQueueResponse(
        items=[_review_item_response(item) for item in result.items],
        total_count=result.total_count,
        limit=result.limit,
        offset=result.offset,
    )


def _review_item_response(item: ReviewItem) -> ReviewItemResponse:
    return ReviewItemResponse(
        review_id=item.review_id,
        invoice_id=item.invoice_id,
        invoice_number=item.invoice_number,
        supplier_tax_number=item.supplier_tax_number,
        supplier_name=item.supplier_name,
        invoice_date=item.invoice_date,
        currency=item.currency,
        total_amount=decimal_to_api(item.total_amount),
        workflow=item.workflow,
        status=item.status,
        review_reasons=[_reason_response(reason) for reason in item.review_reasons],
        warnings=list(item.warnings),
        created_at=item.created_at,
        updated_at=item.updated_at,
        version=item.version,
    )


def _reason_response(reason: ManualReviewReason) -> ManualReviewReasonResponse:
    return ManualReviewReasonResponse(
        code=reason.code,
        message=reason.message,
        line_number=reason.line_number,
        tax_index=reason.tax_index,
        candidate_count=reason.candidate_count,
        source=reason.source,
        details=[[key, value] for key, value in reason.details],
    )


def _acknowledgement_response(
    acknowledgement: ReviewDecisionAcknowledgement,
) -> ReviewDecisionAcknowledgementResponse:
    return ReviewDecisionAcknowledgementResponse(
        accepted=acknowledgement.accepted,
        review_id=acknowledgement.review_id,
        status=acknowledgement.status,
        version=acknowledgement.version,
        decision=acknowledgement.decision,
        selected_workflow=acknowledgement.selected_workflow,
    )


def _decision_ingestion_response(
    result: WorkbenchDecisionIngestionResult,
) -> WorkbenchDecisionIngestionResponse:
    return WorkbenchDecisionIngestionResponse(
        company_id=result.company_id,
        processed_count=result.processed_count,
        already_processed_count=result.already_processed_count,
        acknowledgement_failed_count=result.acknowledgement_failed_count,
        failed_count=result.failed_count,
        results=[
            WorkbenchDecisionIngestionCandidateResponse(
                review_id=item.review_id,
                odoo_record_id=item.odoo_record_id,
                status=item.status,
                acknowledged=item.acknowledged,
                idempotency_key=item.idempotency_key,
                message=item.message,
            )
            for item in result.results
        ],
    )


def _execution_approval(value: ExecutionApprovalRequest | None) -> ExecutionApproval | None:
    if value is None:
        return None
    return ExecutionApproval(approved_by=value.approved_by)


def _vendor_bill_execution_response(
    result: WorkbenchVendorBillExecutionResult,
) -> WorkbenchVendorBillExecutionResponse:
    return WorkbenchVendorBillExecutionResponse(
        review_id=result.review_id,
        company_id=result.company_id,
        decision_version=result.decision_version,
        mode=result.mode,
        status=result.status,
        execution_id=result.execution_id,
        runtime_state=result.runtime_state.value if result.runtime_state is not None else None,
        artifacts=[_artifact_response(artifact) for artifact in result.artifacts],
        message=result.message,
    )


def _quotation_scenario_evidence_response(
    result: WorkbenchQuotationScenarioEvidenceResult,
) -> WorkbenchQuotationScenarioEvidenceResponse:
    return WorkbenchQuotationScenarioEvidenceResponse(
        review_id=result.review_id,
        company_id=result.company_id,
        decision_version=result.decision_version,
        status=result.status,
        decision_id=result.decision_id,
        persisted_scenario_ids=list(result.persisted_scenario_ids),
        message=result.message,
    )


def _vendor_bill_preview_response(preview: VendorBillPreview) -> VendorBillPreviewResponse:
    return VendorBillPreviewResponse(
        review_id=preview.review_id,
        company_id=preview.company_id,
        decision_version=preview.decision_version,
        decision_id=preview.decision_id,
        selected_workflow=preview.selected_workflow,
        move_type=preview.move_type,
        partner_id=preview.partner_id,
        invoice_date=preview.invoice_date,
        reference=preview.reference,
        header_company_id=preview.header_company_id,
        currency_code=preview.currency_code,
        currency_id=preview.currency_id,
        idempotency_key=preview.idempotency_key,
        lines=[
            VendorBillPreviewLineResponse(
                line_number=line.line_number,
                description=line.description,
                quantity=decimal_to_api(line.quantity),
                unit_price=decimal_to_api(line.unit_price),
                account_id=line.account_id,
                product_id=line.product_id,
                tax_ids=list(line.tax_ids),
            )
            for line in preview.lines
        ],
        gross_source_amount=decimal_to_api(preview.gross_source_amount),
        total_discount=decimal_to_api(preview.total_discount),
        preview_untaxed=decimal_to_api(preview.preview_untaxed),
        preview_tax=decimal_to_api(preview.preview_tax),
        preview_total=decimal_to_api(preview.preview_total),
    )


def _artifact_response(artifact: ExecutionArtifact) -> ExecutionArtifactResponse:
    return ExecutionArtifactResponse(
        artifact_type=artifact.artifact_type,
        artifact_id=artifact.artifact_id,
        external_identity=artifact.external_identity,
        created=artifact.created,
    )


def _workbench_execution_status_response(status: WorkbenchExecutionStatus) -> WorkbenchExecutionStatusResponse:
    return WorkbenchExecutionStatusResponse(
        review_id=status.review_id,
        company_id=status.company_id,
        review_version=status.review_version,
        review_status=status.review_status,
        decision=(
            WorkbenchExecutionDecisionResponse(
                decision_id=status.decision.decision_id,
                decision_version=status.decision.decision_version,
                selected_workflow=status.decision.selected_workflow,
            )
            if status.decision is not None
            else None
        ),
        execution=(
            WorkbenchExecutionSummaryResponse(
                execution_id=status.execution.execution_id,
                mode=status.execution.mode,
                state=status.execution.state,
                retry_count=status.execution.retry_count,
                max_attempts=status.execution.max_attempts,
                remaining_attempts=status.execution.remaining_attempts,
                retry_possible=status.execution.retry_possible,
            )
            if status.execution is not None
            else None
        ),
        failure=(
            WorkbenchExecutionFailureResponse(
                step_key=status.failure.step_key,
                error_code=status.failure.error_code,
                safe_message=status.failure.safe_message,
            )
            if status.failure is not None
            else None
        ),
        artifacts=[_artifact_response(artifact) for artifact in status.artifacts],
        evidence=WorkbenchEvidenceStatusResponse(
            stage_one_present=status.evidence.stage_one_present,
            stage_one_review_version=status.evidence.stage_one_review_version,
            stage_two_present=status.evidence.stage_two_present,
            stage_two_decision_version=status.evidence.stage_two_decision_version,
        ),
        authorization=(
            WorkbenchExecutionAuthorizationResponse(
                authorization_id=status.authorization.authorization_id,
                operation_type=status.authorization.operation_type,
                target_version=status.authorization.target_version,
                status=status.authorization.status,
                use_count=status.authorization.use_count,
                is_expired=status.authorization.is_expired,
                consumed_by_execution_id=status.authorization.consumed_by_execution_id,
            )
            if status.authorization is not None
            else None
        ),
        recovery=WorkbenchRecoveryStatusResponse(
            execution_completed=status.recovery.execution_completed,
            waiting_retry=status.recovery.waiting_retry,
            remaining_attempts=status.recovery.remaining_attempts,
        ),
    )


def _success[DataT](
    response: Response,
    trace_id: str,
    data: DataT,
    *,
    warnings: list[str],
) -> ApiEnvelope[DataT]:
    response.headers["X-Trace-ID"] = trace_id
    return ApiEnvelope(success=True, data=data, warnings=warnings, errors=[], trace_id=trace_id)


def _reject_unsupported_query_params(request: Request, allowed_params: frozenset[str]) -> None:
    unsupported = set(request.query_params) - allowed_params
    if unsupported:
        raise WorkbenchContractError("Unsupported Workbench query parameter.")


def _reject_extra_decision_fields(request: ReviewDecisionRequest) -> None:
    if request.model_extra:
        raise WorkbenchContractError("Unsupported Workbench decision field.")


def _raise_error(exc: Exception, *, trace_id: str) -> JSONResponse:
    status_code = _status_code_for_exception(exc)
    code = getattr(exc, "error_category", "internal_error")
    message = getattr(exc, "safe_message", "Internal server error.")
    return error_response_factory(trace_id)(status_code, str(code), str(message))


def _status_code_for_exception(exc: Exception) -> int:
    if isinstance(exc, WriteAuthorizationNotFoundError):
        return HTTPStatus.NOT_FOUND
    if isinstance(
        exc,
        (
            WriteAuthorizationScopeMismatchError,
            WriteAuthorizationExpiredError,
            WriteAuthorizationRevokedError,
            WriteAuthorizationAlreadyConsumedError,
        ),
    ):
        return HTTPStatus.CONFLICT
    if isinstance(exc, WriteAuthorizationError):
        return HTTPStatus.INTERNAL_SERVER_ERROR
    if isinstance(exc, WorkbenchContractError):
        return HTTPStatus.BAD_REQUEST
    if isinstance(exc, ExecutionPlanningError):
        return HTTPStatus.BAD_REQUEST
    if isinstance(
        exc,
        (
            ExecutionPreviewUnsupportedWorkflowError,
            ExecutionPreviewCurrencyResolutionError,
            VendorBillBuildError,
        ),
    ):
        return HTTPStatus.BAD_REQUEST
    if isinstance(exc, ExecutionSourceInvoiceNotFoundError):
        return HTTPStatus.NOT_FOUND
    if isinstance(exc, ExecutionSourceInvoiceIntegrityError):
        return HTTPStatus.CONFLICT
    if isinstance(exc, ExecutionSourceInvoiceError):
        return HTTPStatus.INTERNAL_SERVER_ERROR
    if isinstance(exc, SupplierResolutionContractError):
        return HTTPStatus.BAD_REQUEST
    if isinstance(exc, ProductRemediationContractError):
        return HTTPStatus.BAD_REQUEST
    if isinstance(exc, SupplierPartnerWriteSafetyGateError):
        return HTTPStatus.FORBIDDEN
    if isinstance(exc, ProductWriteSafetyGateError):
        return HTTPStatus.FORBIDDEN
    if isinstance(exc, PermissionDeniedError):
        return HTTPStatus.FORBIDDEN
    if isinstance(exc, (ReviewNotFoundError, SupplierResolutionPartnerNotFoundError)):
        return HTTPStatus.NOT_FOUND
    if isinstance(
        exc,
        (
            ReviewVersionConflictError,
            ReviewStateConflictError,
            ReviewDecisionIdempotencyConflictError,
            SupplierResolutionConflictError,
            SupplierResolutionOneOffVendorNotHubOwnedError,
            SupplierResolutionRaceError,
            SupplierResolutionPartnerMismatchError,
            SupplierResolutionPartnerInactiveError,
            ProductRemediationEligibilityError,
            ProductRemediationSupplierUnresolvedError,
            ProductRemediationConflictError,
            ProductRemediationRaceError,
        ),
    ):
        return HTTPStatus.CONFLICT
    if isinstance(
        exc,
        (
            ReviewDataIntegrityError,
            ReviewDecisionDataIntegrityError,
            ReviewPersistenceError,
            ReviewQueryError,
            ReviewDecisionError,
            SupplierResolutionDataIntegrityError,
            SupplierResolutionError,
            SupplierPartnerWriteError,
            ProductRemediationDataIntegrityError,
            ProductRemediationIdentityAmbiguousError,
            ProductWriteError,
            SupplierInfoWriteError,
        ),
    ):
        return HTTPStatus.INTERNAL_SERVER_ERROR
    return HTTPStatus.INTERNAL_SERVER_ERROR


@router.post(
    "/reviews/{review_id}/write-authorizations",
    response_model=WriteAuthorizationEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Issue one narrow, single-use write authorization",
    description=(
        "Requires workbench_execute. Issues one short-lived (15 minute), single-use authorization scoped to "
        "exactly (company_id, review_id, operation_type, target_version), replacing the need to open a global "
        "write gate/restart the container for one operator-approved write. Supports EXECUTE_VENDOR_BILL, "
        "CREATE_PERMANENT_SUPPLIER, ONE_OFF_VENDOR_SUPPLIER, ONE_OFF_VENDOR_ARCHIVE, and CREATE_NEW_PRODUCT. The "
        "master production kill switch (PRODUCTION_OPERATIONS_ENABLED) and named-approver requirement are never "
        "bypassed."
    ),
)
def issue_write_authorization(
    review_id: str,
    request_body: WriteAuthorizationIssueRequest,
    response: Response,
    context: RequestContextDep,
    use_case: CreateWriteAuthorizationUseCaseDep,
) -> WriteAuthorizationEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_EXECUTE)(context)
        record = use_case.execute(
            company_id=context.company_id,
            review_id=review_id,
            decision_version=request_body.decision_version,
            operation_type=request_body.operation_type,
            authorized_by=context.user_id,
            justification=request_body.justification,
        )
        return _success(response, context.trace_id, WriteAuthorizationResponse.model_validate(record), warnings=[])
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


@router.get(
    "/reviews/{review_id}/write-authorizations",
    response_model=WriteAuthorizationsEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="List auditable Vendor Bill execution authorizations",
)
def list_write_authorizations(
    review_id: str,
    response: Response,
    request: Request,
    context: RequestContextDep,
    use_case: ListWriteAuthorizationsUseCaseDep,
) -> WriteAuthorizationsEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_REVIEW_READ)(context)
        _reject_unsupported_query_params(request, frozenset())
        records = use_case.execute(company_id=context.company_id, review_id=review_id)
        return _success(
            response, context.trace_id, [WriteAuthorizationResponse.model_validate(r) for r in records], warnings=[]
        )
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


@router.post(
    "/reviews/{review_id}/write-authorizations/{authorization_id}/revoke",
    response_model=WriteAuthorizationEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Revoke a Vendor Bill authorization, including further same-execution recovery",
)
def revoke_write_authorization(
    review_id: str,
    authorization_id: str,
    response: Response,
    context: RequestContextDep,
    use_case: RevokeWriteAuthorizationUseCaseDep,
) -> WriteAuthorizationEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_EXECUTE)(context)
        record = use_case.execute(
            company_id=context.company_id,
            review_id=review_id,
            authorization_id=authorization_id,
            revoked_by=context.user_id,
        )
        return _success(response, context.trace_id, WriteAuthorizationResponse.model_validate(record), warnings=[])
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


@router.get(
    "/reviews/{review_id}/one-off-vendor-retirement",
    response_model=OneOffVendorRetirementEnvelope,
    responses=COMMON_ERROR_RESPONSES,
)
def get_one_off_vendor_retirement(
    review_id: str,
    response: Response,
    request: Request,
    context: RequestContextDep,
    use_case: OneOffVendorRetirementUseCaseDep,
    review_version: Annotated[int | None, Query(gt=0)] = None,
) -> OneOffVendorRetirementEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_REVIEW_READ)(context)
        _reject_unsupported_query_params(request, frozenset({"review_version"}))
        retirement = use_case.execute(review_id=review_id, company_id=context.company_id, review_version=review_version)
        return _success(
            response, context.trace_id, OneOffVendorRetirementResponse.model_validate(retirement), warnings=[]
        )
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


@router.post(
    "/reviews/{review_id}/one-off-vendor-retirement/recover",
    response_model=OneOffVendorRetirementRecoveryEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Recover a ONE_OFF_VENDOR retirement using the existing archive-last state machine",
    description=(
        "Requires workbench_execute. Body is only review_version (the retirement row's own persisted version) "
        "and an optional authorization_id from a pre-issued narrow write authorization scoped to exactly "
        "(company_id, review_id, ONE_OFF_VENDOR_ARCHIVE, review_version) -- see the write-authorizations "
        "endpoint. Without it, the write still requires SUPPLIER_REMEDIATION_WRITE_ENABLED to be globally open; "
        "the master production kill switch and named-approver checks are never bypassed by either path. "
        "Invokes the existing, unmodified ArchiveOneOffVendorUseCase exactly once -- ARCHIVE_ATTEMPTED and "
        "NEEDS_RECONCILIATION always read the partner back before ever writing again."
    ),
)
async def recover_one_off_vendor_retirement(
    review_id: str,
    request_body: OneOffVendorRetirementRecoveryRequest,
    response: Response,
    context: RequestContextDep,
    workflow: RecoverOneOffVendorRetirementWorkflowDep,
) -> OneOffVendorRetirementRecoveryEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_EXECUTE)(context)
        result = await workflow.execute(
            ArchiveOneOffVendorCommand(
                review_id=review_id, company_id=context.company_id, review_version=request_body.review_version
            ),
            approved_by=context.user_id,
            authorization_id=request_body.authorization_id,
        )
        return _success(
            response, context.trace_id, OneOffVendorRetirementRecoveryResponse.model_validate(result), warnings=[]
        )
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)
