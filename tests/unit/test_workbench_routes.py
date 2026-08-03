from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from httpx import AsyncClient

from app.api.dependencies import (
    get_list_review_queue_use_case,
    get_request_context,
    get_review_item_use_case,
    get_submit_review_decision_use_case,
)
from app.api.security import (
    AuthenticationMethod,
    InvalidTokenError,
    OidcProviderUnavailableError,
    Permission,
    RequestContext,
)
from app.application.workbench import (
    ReviewDecisionAcknowledgement,
    ReviewDecisionCommand,
    ReviewDecisionType,
    ReviewItem,
    ReviewQueueResult,
    ReviewStatus,
)
from app.application.workbench.exceptions import (
    ReviewDecisionError,
    ReviewDecisionIdempotencyConflictError,
    ReviewNotFoundError,
    ReviewPersistenceError,
    ReviewQueryError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
    WorkbenchContractError,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.main import app


async def test_get_queue_success(api_client: AsyncClient) -> None:
    use_case = FakeListUseCase(_queue_result())

    response = await _get(
        api_client,
        "/api/workbench/reviews",
        context=_context(Permission.WORKBENCH_REVIEW_READ),
        list_use_case=use_case,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["trace_id"] == "trace-123"
    assert response.headers["x-trace-id"] == "trace-123"
    assert body["errors"] == []
    assert body["data"]["items"][0]["total_amount"] == "259.2000"
    assert body["data"]["items"][0]["workflow"] == "manual_review"
    assert body["data"]["items"][0]["created_at"] == "2026-07-17T09:30:00Z"
    assert use_case.calls == 1


async def test_get_queue_exact_query_mapping(api_client: AsyncClient) -> None:
    use_case = FakeListUseCase(_queue_result())

    await _get(
        api_client,
        "/api/workbench/reviews",
        params={
            "status": "dismissed",
            "limit": "25",
            "offset": "10",
            "created_from": "2026-07-17T00:00:00+00:00",
            "created_to": "2026-07-18T00:00:00+00:00",
            "supplier_tax_number": "1234567890",
            "workflow": "vendor_bill",
        },
        context=_context(Permission.WORKBENCH_REVIEW_READ, company_id=44),
        list_use_case=use_case,
    )

    query = use_case.last_query
    assert query.company_id == 44
    assert query.status is ReviewStatus.DISMISSED
    assert query.limit == 25
    assert query.offset == 10
    assert query.supplier_tax_number == "1234567890"
    assert query.workflow is WorkflowType.VENDOR_BILL
    assert query.created_from == datetime(2026, 7, 17, tzinfo=UTC)
    assert query.created_to == datetime(2026, 7, 18, tzinfo=UTC)


async def test_company_id_query_cannot_override_request_context(api_client: AsyncClient) -> None:
    use_case = FakeListUseCase(_queue_result())

    response = await _get(
        api_client,
        "/api/workbench/reviews",
        params={"company_id": "999"},
        context=_context(Permission.WORKBENCH_REVIEW_READ, company_id=7),
        list_use_case=use_case,
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "workbench_contract_error"
    assert use_case.calls == 0


async def test_read_permission_required_for_queue(api_client: AsyncClient) -> None:
    response = await _get(api_client, "/api/workbench/reviews", context=_context())

    assert response.status_code == 403
    assert response.json()["errors"][0]["code"] == "permission_denied"


async def test_get_detail_success_and_company_isolation(api_client: AsyncClient) -> None:
    use_case = FakeGetUseCase(_review_item())

    response = await _get(
        api_client,
        "/api/workbench/reviews/review-1",
        context=_context(Permission.WORKBENCH_REVIEW_READ, company_id=88),
        get_use_case=use_case,
    )

    assert response.status_code == 200
    assert response.json()["data"]["review_id"] == "review-1"
    assert use_case.calls == 1
    assert use_case.last_query.review_id == "review-1"
    assert use_case.last_query.company_id == 88


async def test_get_detail_not_found_maps_to_404_without_cross_company_leak(api_client: AsyncClient) -> None:
    response = await _get(
        api_client,
        "/api/workbench/reviews/review-other-company",
        context=_context(Permission.WORKBENCH_REVIEW_READ),
        get_use_case=FakeGetUseCase(ReviewNotFoundError("Review item was not found.")),
    )

    assert response.status_code == 404
    assert response.json()["errors"] == [{"code": "review_not_found", "message": "Review item was not found."}]


async def test_post_select_workflow_success_maps_path_context_and_body(api_client: AsyncClient) -> None:
    use_case = FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.SELECT_WORKFLOW))

    response = await _post_decision(
        api_client,
        "review-from-path",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE, company_id=51, user_id="finance.user"),
        submit_use_case=use_case,
        json={
            "expected_version": 3,
            "decision": "select_workflow",
            "selected_workflow": "vendor_bill",
            "selected_partner_id": 700,
            "line_resolutions": [{"line_number": "1", "selected_product_id": 800}],
            "tax_resolutions": [{"line_number": "1", "tax_index": 0, "selected_tax_id": 900}],
            "business_context": {"sales_order_id": 1000, "project_id": 1001},
            "comment": "approved by finance",
            "idempotency_key": "decision-key-1",
        },
    )

    command = use_case.last_command
    assert response.status_code == 200
    assert response.json()["data"]["decision"] == "select_workflow"
    assert command.review_id == "review-from-path"
    assert command.company_id == 51
    assert command.decided_by == "finance.user"
    assert command.expected_version == 3
    assert command.idempotency_key == "decision-key-1"
    assert command.selected_workflow is WorkflowType.VENDOR_BILL
    assert command.selected_partner_id == 700
    assert command.line_resolutions[0].selected_product_id == 800
    assert command.tax_resolutions[0].selected_tax_id == 900
    assert command.business_context.sales_order_id == 1000
    assert command.business_context.project_id == 1001
    assert use_case.calls == 1


async def test_post_dismiss_success(api_client: AsyncClient) -> None:
    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        submit_use_case=FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.DISMISS)),
        json={"expected_version": 1, "decision": "dismiss", "comment": "not relevant", "idempotency_key": "key-1"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "dismissed"


async def test_decision_body_cannot_set_identity_or_path_fields(api_client: AsyncClient) -> None:
    for forbidden_field in ("review_id", "company_id", "decided_by"):
        response = await _post_decision(
            api_client,
            "review-1",
            context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
            submit_use_case=FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.DISMISS)),
            json={
                "expected_version": 1,
                "decision": "dismiss",
                "idempotency_key": "key-1",
                forbidden_field: "client-controlled",
            },
        )

        assert response.status_code == 400
        assert response.json()["errors"][0]["message"] == "Unsupported Workbench decision field."


async def test_authenticated_user_overrides_client_identity_attempt(api_client: AsyncClient) -> None:
    use_case = FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.DISMISS))

    await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE, user_id="trusted-user"),
        submit_use_case=use_case,
        json={"expected_version": 1, "decision": "dismiss", "idempotency_key": "key-1", "decided_by": "attacker"},
    )

    assert use_case.calls == 0


async def test_decide_permission_required(api_client: AsyncClient) -> None:
    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_READ),
        json={"expected_version": 1, "decision": "dismiss", "idempotency_key": "key-1"},
    )

    assert response.status_code == 403


async def test_decision_conflicts_map_to_409(api_client: AsyncClient) -> None:
    cases = [
        ReviewVersionConflictError("Review item version does not match expected_version."),
        ReviewStateConflictError("Review item is no longer pending review."),
        ReviewDecisionIdempotencyConflictError("Review decision idempotency key conflicts with an existing decision."),
    ]
    for exc in cases:
        response = await _post_decision(
            api_client,
            "review-1",
            context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
            submit_use_case=FakeSubmitUseCase(exc),
            json={"expected_version": 1, "decision": "dismiss", "idempotency_key": "key-1"},
        )

        assert response.status_code == 409
        assert response.json()["errors"][0]["code"] == exc.error_category


async def test_workbench_contract_error_maps_to_400(api_client: AsyncClient) -> None:
    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        submit_use_case=FakeSubmitUseCase(WorkbenchContractError("selected_workflow is required.")),
        json={"expected_version": 1, "decision": "dismiss", "idempotency_key": "key-1"},
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["message"] == "selected_workflow is required."


async def test_authentication_failures_map_to_401_envelope(api_client: AsyncClient) -> None:
    app.dependency_overrides[get_request_context] = lambda: (_ for _ in ()).throw(
        InvalidTokenError("Bearer token is invalid.")
    )
    try:
        response = await api_client.get(
            "/api/workbench/reviews",
            headers={"Authorization": "Bearer secret-token", "X-Trace-ID": "trace-auth"},
        )
    finally:
        app.dependency_overrides.clear()

    body = response.json()
    assert response.status_code == 401
    assert body["trace_id"] == "trace-auth"
    assert response.headers["x-trace-id"] == "trace-auth"
    assert body["errors"][0]["code"] == "invalid_token"
    assert "secret-token" not in response.text


async def test_provider_unavailable_maps_to_503(api_client: AsyncClient) -> None:
    app.dependency_overrides[get_request_context] = lambda: (_ for _ in ()).throw(
        OidcProviderUnavailableError("OIDC discovery endpoint is unavailable.")
    )
    try:
        response = await api_client.get("/api/workbench/reviews", headers={"X-Trace-ID": "trace-auth"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["errors"][0]["code"] == "oidc_provider_unavailable"


async def test_persistence_query_and_unexpected_failures_are_sanitized(api_client: AsyncClient) -> None:
    cases = [
        ReviewPersistenceError("Review persistence operation failed."),
        ReviewQueryError("Review queue query failed."),
        ReviewDecisionError("Review decision submission failed."),
        RuntimeError("password=secret sql select"),
    ]
    for exc in cases:
        response = await _get(
            api_client,
            "/api/workbench/reviews",
            context=_context(Permission.WORKBENCH_REVIEW_READ),
            list_use_case=FakeListUseCase(exc),
        )

        assert response.status_code == 500
        assert "secret" not in response.text
        assert "sql" not in response.text.lower()
        if isinstance(exc, RuntimeError):
            assert response.json()["errors"][0] == {"code": "internal_error", "message": "Internal server error."}


async def test_response_and_error_envelope_consistency(api_client: AsyncClient) -> None:
    success = await _get(
        api_client,
        "/api/workbench/reviews",
        context=_context(Permission.WORKBENCH_REVIEW_READ),
        list_use_case=FakeListUseCase(_queue_result()),
    )
    failure = await _get(api_client, "/api/workbench/reviews", context=_context())

    assert set(success.json()) == {"success", "data", "warnings", "errors", "trace_id"}
    assert set(failure.json()) == {"success", "data", "warnings", "errors", "trace_id"}
    assert failure.json()["success"] is False
    assert failure.json()["data"] is None
    assert failure.json()["warnings"] == []


async def test_workbench_request_validation_uses_safe_error_envelope(api_client: AsyncClient) -> None:
    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={"expected_version": "not-an-integer", "decision": "dismiss", "idempotency_key": "key-1"},
    )

    body = response.json()
    assert response.status_code == 400
    assert body["success"] is False
    assert body["data"] is None
    assert body["errors"] == [{"code": "request_validation_error", "message": "Request validation failed."}]
    assert body["trace_id"]


async def test_openapi_contains_exactly_three_workbench_routes_and_no_identity_inputs(api_client: AsyncClient) -> None:
    response = await api_client.get("/openapi.json")

    paths = response.json()["paths"]
    workbench_paths = {path: methods for path, methods in paths.items() if path.startswith("/api/workbench")}
    assert set(workbench_paths) == {
        "/api/workbench/reviews",
        "/api/workbench/reviews/{review_id}",
        "/api/workbench/reviews/{review_id}/decision",
    }
    decision_schema = response.json()["components"]["schemas"]["ReviewDecisionRequest"]
    schema_text = str(decision_schema)
    assert "company_id" not in schema_text
    assert "decided_by" not in schema_text
    assert "review_id" not in schema_text
    queue_params = workbench_paths["/api/workbench/reviews"]["get"]["parameters"]
    assert "company_id" not in {param["name"] for param in queue_params}
    assert workbench_paths["/api/workbench/reviews"]["get"]["security"] == [{"HTTPBearer": []}]


def test_workbench_routes_preserve_architecture_boundaries() -> None:
    source = Path("app/api/routers/workbench.py").read_text(encoding="utf-8").lower()

    for token in (
        "sqlalchemy",
        "app.models",
        "app.persistence",
        "app.connectors",
        "app.erp",
        "vendorbillwriter",
        "workflowstrategy",
        "decisionengine",
        "action_post",
        "account.move",
    ):
        assert token not in source


def test_existing_health_endpoint_remains_unchanged() -> None:
    assert 'return {"status": "ok"}' in Path("app/api/routers/health.py").read_text(encoding="utf-8")


class FakeListUseCase:
    def __init__(self, result: ReviewQueueResult | Exception) -> None:
        self.result = result
        self.calls = 0
        self.last_query = None

    def execute(self, query):
        self.calls += 1
        self.last_query = query
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeGetUseCase:
    def __init__(self, result: ReviewItem | Exception) -> None:
        self.result = result
        self.calls = 0
        self.last_query = None

    def execute(self, query):
        self.calls += 1
        self.last_query = query
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeSubmitUseCase:
    def __init__(self, result: ReviewDecisionAcknowledgement | Exception) -> None:
        self.result = result
        self.calls = 0
        self.last_command: ReviewDecisionCommand | None = None

    def execute(self, command: ReviewDecisionCommand) -> ReviewDecisionAcknowledgement:
        self.calls += 1
        self.last_command = command
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def _get(
    api_client: AsyncClient,
    path: str,
    *,
    context: RequestContext,
    list_use_case: FakeListUseCase | None = None,
    get_use_case: FakeGetUseCase | None = None,
    params: dict[str, str] | None = None,
):
    app.dependency_overrides[get_request_context] = lambda: context
    if list_use_case is not None:
        app.dependency_overrides[get_list_review_queue_use_case] = lambda: list_use_case
    if get_use_case is not None:
        app.dependency_overrides[get_review_item_use_case] = lambda: get_use_case
    try:
        return await api_client.get(path, params=params)
    finally:
        app.dependency_overrides.clear()


async def _post_decision(
    api_client: AsyncClient,
    review_id: str,
    *,
    context: RequestContext,
    json: dict[str, Any],
    submit_use_case: FakeSubmitUseCase | None = None,
):
    app.dependency_overrides[get_request_context] = lambda: context
    if submit_use_case is not None:
        app.dependency_overrides[get_submit_review_decision_use_case] = lambda: submit_use_case
    try:
        return await api_client.post(f"/api/workbench/reviews/{review_id}/decision", json=json)
    finally:
        app.dependency_overrides.clear()


def _context(
    *permissions: Permission,
    company_id: int = 7,
    user_id: str = "user-1",
) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        user_name="Finance User",
        company_id=company_id,
        permissions=permissions,
        trace_id="trace-123",
        authentication_method=AuthenticationMethod.JWT,
    )


def _queue_result() -> ReviewQueueResult:
    return ReviewQueueResult(items=(_review_item(),), total_count=1, limit=50, offset=0)


def _review_item() -> ReviewItem:
    return ReviewItem(
        review_id="review-1",
        invoice_id="invoice-1",
        invoice_number="INV-1",
        supplier_tax_number="1234567890",
        supplier_name="Supplier A",
        invoice_date=datetime(2026, 7, 17, tzinfo=UTC).date(),
        currency="TRY",
        total_amount=Decimal("259.2000"),
        workflow=WorkflowType.MANUAL_REVIEW,
        status=ReviewStatus.PENDING_REVIEW,
        review_reasons=(
            ManualReviewReason(
                code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
                message="Product was not found.",
                line_number="1",
                candidate_count=0,
                source="rule_engine",
                details=(("identifier", "SKU-1"),),
            ),
        ),
        warnings=("Check product mapping.",),
        created_at=datetime(2026, 7, 17, 9, 30, tzinfo=UTC),
        updated_at=datetime(2026, 7, 17, 9, 35, tzinfo=UTC),
        version=1,
    )


def _acknowledgement(decision: ReviewDecisionType) -> ReviewDecisionAcknowledgement:
    return ReviewDecisionAcknowledgement(
        accepted=True,
        review_id="review-1",
        status=ReviewStatus.DECISION_SUBMITTED
        if decision is ReviewDecisionType.SELECT_WORKFLOW
        else ReviewStatus.DISMISSED,
        version=2,
        decision=decision,
        selected_workflow=WorkflowType.VENDOR_BILL if decision is ReviewDecisionType.SELECT_WORKFLOW else None,
        warnings=("Persisted only.",),
    )
