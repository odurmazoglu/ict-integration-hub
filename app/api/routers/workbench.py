from __future__ import annotations

from datetime import datetime
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer

from app.api.dependencies import (
    GetReviewItemUseCaseDep,
    ListReviewQueueUseCaseDep,
    RequestContextDep,
    ResolveWorkbenchSupplierUseCaseDep,
    SubmitReviewDecisionUseCaseDep,
    WorkbenchAcceptedDecisionExecutionDispatcherDep,
    WorkbenchDecisionIngestionWorkflowDep,
    WorkbenchQuotationScenarioEvidenceWorkflowDep,
)
from app.api.error_handling import error_response_factory
from app.api.security import Permission, PermissionDeniedError, require_permission
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
    SupplierResolutionPartnerInactiveError,
    SupplierResolutionPartnerMismatchError,
    SupplierResolutionPartnerNotFoundError,
    WorkbenchContractError,
)
from app.application.workbench.supplier_remediation import ResolveWorkbenchSupplierCommand
from app.application.workflow import ManualReviewReason, WorkflowType
from app.schemas.workbench import (
    ApiEnvelope,
    BusinessContextAllocationRequest,
    BusinessContextAllocationSetRequest,
    ExecutionApprovalRequest,
    ExecutionArtifactResponse,
    LineResolutionRequest,
    ManualReviewReasonResponse,
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
    WorkbenchDecisionIngestionCandidateResponse,
    WorkbenchDecisionIngestionEnvelope,
    WorkbenchDecisionIngestionResponse,
    WorkbenchQuotationScenarioEvidenceEnvelope,
    WorkbenchQuotationScenarioEvidenceRequest,
    WorkbenchQuotationScenarioEvidenceResponse,
    WorkbenchVendorBillExecutionEnvelope,
    WorkbenchVendorBillExecutionRequest,
    WorkbenchVendorBillExecutionResponse,
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
        "explicit resolution (match an existing partner, create a permanent partner, or one-off) and triggers the "
        "non-destructive SUPPLIER_RESOLUTION reclassification. Legal supplier identity comes only from the review's "
        "immutable source evidence -- never the request body. CREATE_PERMANENT_SUPPLIER is gated by "
        "SUPPLIER_REMEDIATION_WRITE_ENABLED. It never executes a Vendor Bill."
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


def _supplier_remediation_response(result) -> SupplierRemediationResponse:
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
    return LineResolution(line_number=value.line_number, selected_product_id=value.selected_product_id)


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


def _artifact_response(artifact: ExecutionArtifact) -> ExecutionArtifactResponse:
    return ExecutionArtifactResponse(
        artifact_type=artifact.artifact_type,
        artifact_id=artifact.artifact_id,
        external_identity=artifact.external_identity,
        created=artifact.created,
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
    if isinstance(exc, WorkbenchContractError):
        return HTTPStatus.BAD_REQUEST
    if isinstance(exc, ExecutionPlanningError):
        return HTTPStatus.BAD_REQUEST
    if isinstance(exc, SupplierResolutionContractError):
        return HTTPStatus.BAD_REQUEST
    if isinstance(exc, SupplierPartnerWriteSafetyGateError):
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
            SupplierResolutionPartnerMismatchError,
            SupplierResolutionPartnerInactiveError,
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
        ),
    ):
        return HTTPStatus.INTERNAL_SERVER_ERROR
    return HTTPStatus.INTERNAL_SERVER_ERROR
