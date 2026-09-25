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
    GetProductPurchaseAccountUseCaseDep,
    GetReviewItemUseCaseDep,
    ListCategoryPurchaseAccountsUseCaseDep,
    ListExpenseAccountCandidatesUseCaseDep,
    ListReviewQueueUseCaseDep,
    ListWriteAuthorizationsUseCaseDep,
    OneOffVendorRetirementUseCaseDep,
    RebuildReviewExecutionEvidenceUseCaseDep,
    RecoverOneOffVendorRetirementWorkflowDep,
    RequestContextDep,
    ResolveWorkbenchSupplierUseCaseDep,
    RevokeWriteAuthorizationUseCaseDep,
    SubmitOperatingExpenseMappingUseCaseDep,
    SubmitPurchasePurposeUseCaseDep,
    SubmitReviewAccountingResolutionUseCaseDep,
    SubmitReviewDecisionUseCaseDep,
    VendorBillPreviewUseCaseDep,
    VendorBillReadbackUseCaseDep,
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
    ExecutionPreviewResaleAccountingError,
    ExecutionPreviewUnsupportedWorkflowError,
    ExecutionSourceInvoiceError,
    ExecutionSourceInvoiceIntegrityError,
    ExecutionSourceInvoiceNotFoundError,
)
from app.application.execution.vendor_bill_preview import (
    PreviewVendorBillRequest,
    VendorBillPreview,
    VendorBillPreviewResaleAccounting,
)
from app.application.expense_mapping import (
    OperatingExpenseMappingConflictError,
    OperatingExpenseMappingContractError,
    OperatingExpenseMappingDataIntegrityError,
    OperatingExpenseMappingError,
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
from app.application.workbench.accounting_resolution import (
    AccountingTreatmentType,
    SubmitReviewAccountingResolutionCommand,
)
from app.application.workbench.exceptions import (
    AccountingResolutionConflictError,
    AccountingResolutionEligibilityError,
    AccountingResolutionError,
    AccountingResolutionPurposeRequiredError,
    AccountingResolutionPurposeUnsupportedError,
    ExecutionEvidenceRecoveryBuildError,
    ExecutionEvidenceRecoveryConflictError,
    ExecutionEvidenceRecoveryEligibilityError,
    ExecutionEvidenceRecoveryError,
    ExecutionEvidenceRecoveryMismatchError,
    ExecutionEvidenceRecoverySourceMissingError,
    OperatingExpenseMappingAccountInvalidError,
    OperatingExpenseMappingEligibilityError,
    OperatingExpenseMappingSupplierUnresolvedError,
    OperatingExpenseMappingWorkflowError,
    ProductRemediationConflictError,
    ProductRemediationContractError,
    ProductRemediationDataIntegrityError,
    ProductRemediationEligibilityError,
    ProductRemediationIdentityAmbiguousError,
    ProductRemediationRaceError,
    ProductRemediationSupplierUnresolvedError,
    ProductRemediationVerificationError,
    PurchaseAccountCompanyContextError,
    PurchaseAccountDiscoveryError,
    PurchaseAccountProductNotFoundError,
    PurchasePurposeConflictError,
    PurchasePurposeEligibilityError,
    PurchasePurposeError,
    ResaleDecisionEligibilityError,
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
from app.application.workbench.execution_evidence_recovery import RebuildExecutionEvidenceCommand
from app.application.workbench.execution_status import WorkbenchExecutionStatus
from app.application.workbench.expense_account_lookup import ListExpenseAccountCandidatesQuery
from app.application.workbench.one_off_vendor_retirement import ArchiveOneOffVendorCommand, OneOffVendorRetirementStatus
from app.application.workbench.operating_expense_mapping_command import SubmitOperatingExpenseMappingCommand
from app.application.workbench.product_remediation import CreateNewProductCommand, ProductRemediationStatus
from app.application.workbench.purchase_account_discovery import (
    CategoryPurchaseAccountConfiguration,
    GetProductPurchaseAccountQuery,
    ListCategoryPurchaseAccountsQuery,
    ProductPurchaseAccountResolution,
    PurchaseAccountView,
    ResolvedPurchaseAccount,
)
from app.application.workbench.purchase_purpose import SubmitPurchasePurposeCommand
from app.application.workbench.supplier_remediation import ResolveWorkbenchSupplierCommand
from app.application.workbench.vendor_bill_readback import (
    VendorBillReadback,
    VendorBillReadbackError,
    VendorBillReadbackIntegrityError,
    VendorBillReadbackNotFoundError,
    VendorBillReadbackUnavailableError,
    VendorBillResaleReadbackVerification,
)
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
    AccountingResolutionEnvelope,
    AccountingResolutionRequest,
    AccountingResolutionResponse,
    ApiEnvelope,
    BusinessContextAllocationRequest,
    BusinessContextAllocationSetRequest,
    CategoryPurchaseAccountResponse,
    CategoryPurchaseAccountsEnvelope,
    ExecutionApprovalRequest,
    ExecutionArtifactResponse,
    ExecutionEvidenceRecoveryEnvelope,
    ExecutionEvidenceRecoveryRequest,
    ExecutionEvidenceRecoveryResponse,
    ExpenseAccountCandidateResponse,
    ExpenseAccountCandidatesEnvelope,
    LineResolutionRequest,
    ManualReviewReasonResponse,
    OneOffVendorRetirementEnvelope,
    OneOffVendorRetirementRecoveryEnvelope,
    OneOffVendorRetirementRecoveryRequest,
    OneOffVendorRetirementRecoveryResponse,
    OneOffVendorRetirementResponse,
    OperatingExpenseMappingEnvelope,
    OperatingExpenseMappingRequest,
    OperatingExpenseMappingResponse,
    ProductMatchEvidenceResponse,
    ProductPurchaseAccountEnvelope,
    ProductPurchaseAccountResponse,
    ProductRemediationEnvelope,
    ProductRemediationResponse,
    ProductResolutionRequest,
    PurchaseAccountResponse,
    PurchasePurposeEnvelope,
    PurchasePurposeRequest,
    PurchasePurposeResponse,
    ResolvedPurchaseAccountResponse,
    ReviewDecisionAcknowledgementEnvelope,
    ReviewDecisionAcknowledgementResponse,
    ReviewDecisionRequest,
    ReviewEvidenceResponse,
    ReviewItemEnvelope,
    ReviewItemResponse,
    ReviewQueueEnvelope,
    ReviewQueueResponse,
    SourceLineResponse,
    SourceTaxResponse,
    SupplierCandidateResponse,
    SupplierRemediationEnvelope,
    SupplierRemediationResponse,
    SupplierResolutionRequest,
    TaxResolutionRequest,
    VendorBillPreviewEnvelope,
    VendorBillPreviewLineResponse,
    VendorBillPreviewResaleAccountingResponse,
    VendorBillPreviewResponse,
    VendorBillReadbackEnvelope,
    VendorBillReadbackLineResponse,
    VendorBillReadbackResponse,
    VendorBillResaleReadbackLineResponse,
    VendorBillResaleReadbackResponse,
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


@router.get(
    "/expense-accounts",
    response_model=ExpenseAccountCandidatesEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="List eligible Odoo operating-expense accounts",
    description=(
        "Requires workbench_expense_account_read. Read-only, company-scoped (from RequestContext, never the "
        "caller) lookup of Odoo account.account rows eligible for an operating-expense mapping. Not a generic "
        "Odoo browser: the target model, base domain (company + eligible account type), and field list are all "
        "server-controlled; the caller may only supply an optional free-text query filter."
    ),
)
def list_expense_account_candidates(
    response: Response,
    context: RequestContextDep,
    use_case: ListExpenseAccountCandidatesUseCaseDep,
    query: Annotated[str | None, Query()] = None,
) -> ExpenseAccountCandidatesEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_EXPENSE_ACCOUNT_READ)(context)
        candidates = use_case.execute(ListExpenseAccountCandidatesQuery(company_id=context.company_id, query=query))
        return _success(
            response,
            context.trace_id,
            [_expense_account_candidate_response(candidate) for candidate in candidates],
            warnings=[],
        )
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


@router.get(
    "/resale-product-categories",
    response_model=CategoryPurchaseAccountsEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Discover Odoo product-category purchase-account configuration",
    description=(
        "Requires workbench_expense_account_read. Read-only, company-scoped (from RequestContext) discovery of "
        "every Odoo product.category and the purchase/expense account its own configuration "
        "(property_account_expense_categ_id) names, with that account's validity for the company. This is a "
        "category's configured account only -- not any product's final account: product-level overrides and "
        "fiscal positions are not applied here. No approval or allowlist semantics; accepts no query parameters. "
        "Fails closed (409) when Odoo's company context cannot be proven to be the requesting company."
    ),
)
def list_resale_product_categories(
    request: Request,
    response: Response,
    context: RequestContextDep,
    use_case: ListCategoryPurchaseAccountsUseCaseDep,
) -> CategoryPurchaseAccountsEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_EXPENSE_ACCOUNT_READ)(context)
        _reject_unsupported_query_params(request, frozenset())
        categories = use_case.execute(ListCategoryPurchaseAccountsQuery(company_id=context.company_id))
        return _success(
            response,
            context.trace_id,
            [_category_purchase_account_response(category) for category in categories],
            warnings=[],
        )
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


@router.get(
    "/products/{product_id}/purchase-account",
    response_model=ProductPurchaseAccountEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Discover one Odoo product's pre-fiscal-position purchase account",
    description=(
        "Requires workbench_expense_account_read. Read-only, company-scoped (from RequestContext) resolution of "
        "the purchase account Odoo product/category configuration gives one product: a product-level override "
        "wins (never falling back when unusable), otherwise the category account. The account is presented only "
        "when determinable; any doubt is reported as blockers instead. Fiscal-position mapping is never "
        "evaluated, so this is at most the pre-fiscal-position account. Accepts no query parameters."
    ),
)
def get_product_purchase_account(
    product_id: int,
    request: Request,
    response: Response,
    context: RequestContextDep,
    use_case: GetProductPurchaseAccountUseCaseDep,
) -> ProductPurchaseAccountEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_EXPENSE_ACCOUNT_READ)(context)
        _reject_unsupported_query_params(request, frozenset())
        resolution = use_case.execute(
            GetProductPurchaseAccountQuery(company_id=context.company_id, product_id=product_id)
        )
        return _success(response, context.trace_id, _product_purchase_account_response(resolution), warnings=[])
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


@router.get(
    "/reviews/{review_id}/vendor-bill-readback",
    response_model=VendorBillReadbackEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Read back an executed Vendor Bill from its persisted Hub artifact",
    description=(
        "Requires workbench_execute. Resolves exactly one Vendor Bill artifact from the review's latest completed "
        "EXECUTE-mode Hub snapshot, derives the Odoo Vendor Bill id server-side, and reads only fixed Vendor Bill "
        "header and invoice-line fields. The caller cannot provide an Odoo id, model, domain, field list, company, "
        "or partner. Performs zero Hub writes and zero Odoo writes."
    ),
)
def get_vendor_bill_readback(
    review_id: str,
    request: Request,
    response: Response,
    context: RequestContextDep,
    use_case: VendorBillReadbackUseCaseDep,
) -> VendorBillReadbackEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_EXECUTE)(context)
        _reject_unsupported_query_params(request, frozenset())
        readback = use_case.execute(review_id=review_id, company_id=context.company_id)
        return _success(response, context.trace_id, _vendor_bill_readback_response(readback), warnings=[])
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
    "/reviews/{review_id}/operating-expense-mapping",
    response_model=OperatingExpenseMappingEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Configure an operating-expense mapping for a review's resolved supplier",
    description=(
        "Requires workbench_review_decide. For a review whose reasons include OPERATING_EXPENSE_MAPPING_REQUIRED "
        "and whose supplier is already resolved (see the supplier-resolution endpoint), onboards a durable "
        "(company, resolved supplier) -> expense-account mapping via the existing "
        "OnboardOperatingExpenseMappingUseCase, then triggers the non-destructive reclassification that lets the "
        "review reach a submittable decision. vendor_partner_id is never accepted from the caller -- it comes "
        "only from the review's own accepted supplier-resolution effect. The selected expense_account_id is "
        "re-validated read-only against Odoo (exists, eligible operating-expense type, company-scoped) before "
        "anything is persisted. This endpoint never executes a Vendor Bill and never writes to Odoo."
    ),
)
async def submit_operating_expense_mapping(
    review_id: str,
    request_body: OperatingExpenseMappingRequest,
    response: Response,
    context: RequestContextDep,
    use_case: SubmitOperatingExpenseMappingUseCaseDep,
) -> OperatingExpenseMappingEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_REVIEW_DECIDE)(context)
        result = await use_case.execute(
            SubmitOperatingExpenseMappingCommand(
                review_id=review_id,
                company_id=context.company_id,
                expected_version=request_body.expected_version,
                expense_account_id=request_body.expense_account_id,
                expense_category=request_body.expense_category,
                approved_by=context.user_name or context.user_id,
                note=request_body.note,
            )
        )
        return _success(
            response,
            context.trace_id,
            _operating_expense_mapping_response(result),
            warnings=[],
        )
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


@router.post(
    "/reviews/{review_id}/purchase-purpose",
    response_model=PurchasePurposeEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Record why a review's purchase was made",
    description=(
        "Requires workbench_review_decide. For a review whose reasons include an operating-expense-shaped "
        "blocker, records an immutable, review-scoped statement of purchase purpose (INTERNAL_USE, RESALE, "
        "CUSTOMER_PROJECT, OTHER_OPERATING_EXPENSE). This is a business fact, never an accounting decision -- "
        "recording it never reclassifies the review or advances its version by itself. It exists only as the "
        "required precondition for the accounting-resolution endpoint, which some purposes (RESALE, "
        "CUSTOMER_PROJECT) do not yet support."
    ),
)
def submit_purchase_purpose(
    review_id: str,
    request_body: PurchasePurposeRequest,
    response: Response,
    context: RequestContextDep,
    use_case: SubmitPurchasePurposeUseCaseDep,
) -> PurchasePurposeEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_REVIEW_DECIDE)(context)
        result = use_case.execute(
            SubmitPurchasePurposeCommand(
                review_id=review_id,
                company_id=context.company_id,
                expected_version=request_body.expected_version,
                purchase_purpose=request_body.purchase_purpose,
                approved_by=context.user_name or context.user_id,
                note=request_body.note,
            )
        )
        return _success(
            response,
            context.trace_id,
            _purchase_purpose_response(result),
            warnings=[],
        )
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


@router.post(
    "/reviews/{review_id}/accounting-resolution",
    response_model=AccountingResolutionEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Resolve how a review-scoped purchase should be posted, without a supplier-wide mapping",
    description=(
        "Requires workbench_review_decide. The review-scoped escape hatch for a mixed-purpose supplier: unlike "
        "the operating-expense-mapping endpoint, this never writes to the supplier-wide operating_expense_mappings "
        "table -- the resolution applies only to this exact review version. Requires an accepted purchase-purpose "
        "resolution to already exist for this exact review version; RESALE/CUSTOMER_PROJECT purposes are rejected "
        "with a precise not-yet-implemented error rather than silently treated as a plain expense. Only "
        "treatment_type=expense_account is supported today. The selected expense_account_id is re-validated "
        "read-only against Odoo before anything is persisted, then the non-destructive reclassification that lets "
        "the review reach a submittable decision is triggered. This endpoint never executes a Vendor Bill and "
        "never writes to Odoo."
    ),
)
async def submit_review_accounting_resolution(
    review_id: str,
    request_body: AccountingResolutionRequest,
    response: Response,
    context: RequestContextDep,
    use_case: SubmitReviewAccountingResolutionUseCaseDep,
) -> AccountingResolutionEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_REVIEW_DECIDE)(context)
        result = await use_case.execute(
            SubmitReviewAccountingResolutionCommand(
                review_id=review_id,
                company_id=context.company_id,
                expected_version=request_body.expected_version,
                treatment_type=AccountingTreatmentType(request_body.treatment_type),
                expense_account_id=request_body.expense_account_id,
                expense_category=request_body.expense_category,
                approved_by=context.user_name or context.user_id,
                note=request_body.note,
            )
        )
        return _success(
            response,
            context.trace_id,
            _accounting_resolution_response(result),
            warnings=[],
        )
    except Exception as exc:
        return _raise_error(exc, trace_id=context.trace_id)


@router.post(
    "/reviews/{review_id}/execution-evidence/rebuild",
    response_model=ExecutionEvidenceRecoveryEnvelope,
    responses=COMMON_ERROR_RESPONSES,
    summary="Repair a pending review's derived Stage-1 execution evidence",
    description=(
        "Requires workbench_review_decide. A narrow recovery/repair operation, never a generic reclassification: "
        "it recomputes this review's current effective classification using the exact same authoritative "
        "computation reclassification itself uses, and only when that recomputation reproduces exactly the "
        "review's already-persisted current reasons and workflow does it persist the missing/stale Stage-1 "
        "WorkbenchReviewExecutionEvidence for the review's CURRENT version. It never advances the review version, "
        "never changes workflow/reasons, never touches any supplier remediation effect / purchase purpose / "
        "accounting resolution, never writes a supplier-wide operating-expense mapping, and never writes Odoo. "
        "Idempotent: if matching evidence already exists, returns already_applied=true; if existing evidence "
        "conflicts with the freshly recomputed evidence, fails closed rather than overwriting it."
    ),
)
async def rebuild_review_execution_evidence(
    review_id: str,
    request_body: ExecutionEvidenceRecoveryRequest,
    response: Response,
    context: RequestContextDep,
    use_case: RebuildReviewExecutionEvidenceUseCaseDep,
) -> ExecutionEvidenceRecoveryEnvelope | JSONResponse:
    try:
        context = require_permission(Permission.WORKBENCH_REVIEW_DECIDE)(context)
        result = await use_case.execute(
            RebuildExecutionEvidenceCommand(
                review_id=review_id,
                company_id=context.company_id,
                expected_version=request_body.expected_version,
            )
        )
        return _success(
            response,
            context.trace_id,
            _execution_evidence_recovery_response(result),
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
        "explicit operator actions. Optional categ_id is an exact Odoo product.category id, validated read-only "
        "before any write; when the review's current-version purchase purpose is RESALE it is required, must be in "
        "RESALE_PRODUCT_CATEGORY_IDS, and the product must be non-storable."
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
                categ_id=request_body.categ_id,
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


def _expense_account_candidate_response(candidate) -> ExpenseAccountCandidateResponse:
    return ExpenseAccountCandidateResponse(
        id=candidate.id,
        code=candidate.code,
        name=candidate.name,
        account_type=candidate.account_type,
    )


def _purchase_account_response(view: PurchaseAccountView | None) -> PurchaseAccountResponse | None:
    if view is None:
        return None
    return PurchaseAccountResponse(
        account_id=view.account_id,
        code=view.code,
        name=view.name,
        account_type=view.account_type,
        deprecated=view.deprecated,
    )


def _resolved_purchase_account_response(resolved: ResolvedPurchaseAccount) -> ResolvedPurchaseAccountResponse:
    return ResolvedPurchaseAccountResponse(
        status=resolved.status.value,
        configured_account_id=resolved.configured_account_id,
        account=_purchase_account_response(resolved.account),
    )


def _category_purchase_account_response(
    category: CategoryPurchaseAccountConfiguration,
) -> CategoryPurchaseAccountResponse:
    return CategoryPurchaseAccountResponse(
        category_id=category.category_id,
        category_name=category.category_name,
        category_complete_name=category.category_complete_name,
        purchase_account=_resolved_purchase_account_response(category.purchase_account),
    )


def _product_purchase_account_response(
    resolution: ProductPurchaseAccountResolution,
) -> ProductPurchaseAccountResponse:
    return ProductPurchaseAccountResponse(
        product_id=resolution.product_id,
        product_template_id=resolution.product_template_id,
        product_name=resolution.product_name,
        product_active=resolution.product_active,
        product_company_id=resolution.product_company_id,
        product_type=resolution.product_type,
        is_storable=resolution.is_storable,
        category=(
            _category_purchase_account_response(resolution.category) if resolution.category is not None else None
        ),
        product_override=_resolved_purchase_account_response(resolution.product_override),
        pre_fiscal_position_account=_purchase_account_response(resolution.pre_fiscal_position_account),
        pre_fiscal_position_account_source=(
            resolution.pre_fiscal_position_account_source.value
            if resolution.pre_fiscal_position_account_source is not None
            else None
        ),
        pre_fiscal_position_account_determinable=resolution.pre_fiscal_position_account_determinable,
        blockers=[blocker.value for blocker in resolution.blockers],
        fiscal_position_mapping=resolution.fiscal_position_mapping.value,
    )


def _operating_expense_mapping_response(result) -> OperatingExpenseMappingResponse:
    return OperatingExpenseMappingResponse(
        review_id=result.review_id,
        company_id=result.company_id,
        resolution_status=result.status,
        previous_version=result.previous_version,
        current_version=result.current_version,
        current_workflow=result.current_workflow,
        current_review_reasons=[_reason_response(reason) for reason in result.current_review_reasons],
        vendor_partner_id=result.vendor_partner_id,
        expense_account_id=result.expense_account_id,
        expense_category=result.expense_category,
        mapping_outcome=result.mapping_outcome,
        reclassified=result.reclassified,
        already_applied=result.already_applied,
        safe_message=result.safe_message,
    )


def _purchase_purpose_response(result) -> PurchasePurposeResponse:
    return PurchasePurposeResponse(
        review_id=result.review_id,
        company_id=result.company_id,
        review_version=result.review_version,
        purchase_purpose=result.purchase_purpose,
        already_applied=result.already_applied,
        safe_message=result.safe_message,
    )


def _accounting_resolution_response(result) -> AccountingResolutionResponse:
    return AccountingResolutionResponse(
        review_id=result.review_id,
        company_id=result.company_id,
        resolution_status=result.status,
        previous_version=result.previous_version,
        current_version=result.current_version,
        current_workflow=result.current_workflow,
        current_review_reasons=[_reason_response(reason) for reason in result.current_review_reasons],
        treatment_type=result.treatment_type,
        expense_account_id=result.expense_account_id,
        expense_category=result.expense_category,
        reclassified=result.reclassified,
        already_applied=result.already_applied,
        safe_message=result.safe_message,
    )


def _execution_evidence_recovery_response(result) -> ExecutionEvidenceRecoveryResponse:
    return ExecutionEvidenceRecoveryResponse(
        review_id=result.review_id,
        company_id=result.company_id,
        review_version=result.review_version,
        already_applied=result.already_applied,
        partner_id=result.partner_id,
        expense_account_id=result.expense_account_id,
        expense_category=result.expense_category,
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
        evidence=_evidence_response(item.evidence),
    )


def _evidence_response(evidence) -> ReviewEvidenceResponse | None:
    if evidence is None:
        return None
    return ReviewEvidenceResponse(
        supplier_candidates=[
            SupplierCandidateResponse(
                partner_id=candidate.partner_id,
                name=candidate.name,
                vat=candidate.vat,
                active=candidate.active,
                company_type=candidate.company_type,
                parent_id=candidate.parent_id,
                commercial_partner_id=candidate.commercial_partner_id,
                street=candidate.street,
                street2=candidate.street2,
                zip=candidate.zip_code,
                city=candidate.city,
                state_id=candidate.state_id,
                country_id=candidate.country_id,
                email=candidate.email,
                phone=candidate.phone,
                mobile=candidate.mobile,
                website=candidate.website,
                supplier_rank=candidate.supplier_rank,
                customer_rank=candidate.customer_rank,
                company_id=candidate.company_id,
            )
            for candidate in evidence.supplier_candidates
        ],
        source_lines=[
            SourceLineResponse(
                line_number=line.line_number,
                description=line.description,
                quantity=decimal_to_api(line.quantity),
                unit_code=line.unit_code,
                unit_price=decimal_to_api(line.unit_price),
                gross_amount=decimal_to_api(line.gross_amount),
                discount_amount=decimal_to_api(line.discount_amount),
                net_amount=decimal_to_api(line.net_amount),
                taxes=[
                    SourceTaxResponse(
                        tax_type=tax_type,
                        rate=decimal_to_api(rate),
                        tax_amount=decimal_to_api(tax_amount),
                    )
                    for tax_type, rate, tax_amount in line.taxes
                ],
                seller_item_code=line.seller_item_code,
                buyer_item_code=line.buyer_item_code,
                product_match=(
                    ProductMatchEvidenceResponse(
                        status=line.product_match.status,
                        product_id=line.product_match.product_id,
                        matched_by=line.product_match.matched_by,
                        reason=line.product_match.reason,
                        candidate_count=line.product_match.candidate_count,
                        confidence=decimal_to_api(line.product_match.confidence),
                    )
                    if line.product_match is not None
                    else None
                ),
            )
            for line in evidence.source_lines
        ],
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
                resale_accounting=_preview_resale_accounting_response(line.resale_accounting),
            )
            for line in preview.lines
        ],
        gross_source_amount=decimal_to_api(preview.gross_source_amount),
        total_discount=decimal_to_api(preview.total_discount),
        preview_untaxed=decimal_to_api(preview.preview_untaxed),
        preview_tax=decimal_to_api(preview.preview_tax),
        preview_total=decimal_to_api(preview.preview_total),
    )


def _preview_resale_accounting_response(
    pinned: VendorBillPreviewResaleAccounting | None,
) -> VendorBillPreviewResaleAccountingResponse | None:
    if pinned is None:
        return None
    return VendorBillPreviewResaleAccountingResponse(
        product_id=pinned.product_id,
        product_categ_id=pinned.product_categ_id,
        product_categ_name=pinned.product_categ_name,
        account_id=pinned.account_id,
        account_code=pinned.account_code,
        account_name=pinned.account_name,
        account_type=pinned.account_type,
        accounting_source=pinned.accounting_source.value,
        fiscal_position_mapping=pinned.fiscal_position_mapping.value,
    )


def _vendor_bill_readback_response(readback: VendorBillReadback) -> VendorBillReadbackResponse:
    header = readback.header
    return VendorBillReadbackResponse(
        review_id=readback.review_id,
        execution_id=readback.execution_id,
        artifact_id=readback.artifact_id,
        move_id=header.move_id,
        state=header.state,
        move_type=header.move_type,
        partner_id=header.partner_id,
        currency=header.currency,
        amount_untaxed=decimal_to_api(header.amount_untaxed),
        amount_tax=decimal_to_api(header.amount_tax),
        amount_total=decimal_to_api(header.amount_total),
        lines=[
            VendorBillReadbackLineResponse(
                line_id=line.line_id,
                account_id=line.account_id,
                product_id=line.product_id,
                quantity=decimal_to_api(line.quantity),
                price_unit=decimal_to_api(line.price_unit),
                tax_ids=list(line.tax_ids),
                price_subtotal=decimal_to_api(line.price_subtotal),
                price_total=decimal_to_api(line.price_total),
            )
            for line in readback.lines
        ],
        resale_verification=_resale_readback_response(readback.resale_verification),
    )


def _resale_readback_response(
    verification: VendorBillResaleReadbackVerification | None,
) -> VendorBillResaleReadbackResponse | None:
    if verification is None:
        return None
    return VendorBillResaleReadbackResponse(
        status=verification.status.value,
        pin_review_version=verification.pin_review_version,
        fiscal_position_supported=verification.fiscal_position_supported,
        fiscal_position_id=verification.fiscal_position_id,
        lines=[
            VendorBillResaleReadbackLineResponse(
                line_id=line.line_id,
                product_id=line.product_id,
                account_id=line.account_id,
                expected_account_id=line.expected_account_id,
                product_matches=line.product_matches,
                account_matches=line.account_matches,
            )
            for line in verification.lines
        ],
        mismatches=list(verification.mismatches),
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
    if isinstance(exc, VendorBillReadbackNotFoundError):
        return HTTPStatus.NOT_FOUND
    if isinstance(exc, (VendorBillReadbackUnavailableError, VendorBillReadbackIntegrityError)):
        return HTTPStatus.CONFLICT
    if isinstance(exc, VendorBillReadbackError):
        return HTTPStatus.INTERNAL_SERVER_ERROR
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
    if isinstance(exc, PurchaseAccountProductNotFoundError):
        return HTTPStatus.NOT_FOUND
    if isinstance(exc, PurchaseAccountCompanyContextError):
        return HTTPStatus.CONFLICT
    if isinstance(exc, PurchaseAccountDiscoveryError):
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
    if isinstance(exc, ExecutionPreviewResaleAccountingError):
        return HTTPStatus.CONFLICT
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
    if isinstance(exc, (OperatingExpenseMappingContractError, OperatingExpenseMappingAccountInvalidError)):
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
            ProductRemediationVerificationError,
            OperatingExpenseMappingEligibilityError,
            OperatingExpenseMappingSupplierUnresolvedError,
            OperatingExpenseMappingConflictError,
            PurchasePurposeEligibilityError,
            PurchasePurposeConflictError,
            ResaleDecisionEligibilityError,
            AccountingResolutionEligibilityError,
            AccountingResolutionPurposeRequiredError,
            AccountingResolutionPurposeUnsupportedError,
            AccountingResolutionConflictError,
            ExecutionEvidenceRecoveryEligibilityError,
            ExecutionEvidenceRecoverySourceMissingError,
            ExecutionEvidenceRecoveryMismatchError,
            ExecutionEvidenceRecoveryConflictError,
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
            OperatingExpenseMappingWorkflowError,
            OperatingExpenseMappingDataIntegrityError,
            OperatingExpenseMappingError,
            PurchasePurposeError,
            AccountingResolutionError,
            ExecutionEvidenceRecoveryBuildError,
            ExecutionEvidenceRecoveryError,
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
