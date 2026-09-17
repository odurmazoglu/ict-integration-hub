from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from httpx import AsyncClient

from app.api.dependencies import (
    get_create_new_product_use_case,
    get_list_review_queue_use_case,
    get_request_context,
    get_review_item_use_case,
    get_submit_review_decision_use_case,
    get_workbench_accepted_decision_execution_dispatcher,
    get_workbench_decision_ingestion_workflow,
    get_workbench_quotation_scenario_evidence_workflow,
)
from app.api.security import (
    AuthenticationMethod,
    InvalidTokenError,
    OidcProviderUnavailableError,
    Permission,
    RequestContext,
)
from app.application.exceptions.product_remediation import ProductWriteSafetyGateError
from app.application.execution import (
    ExecutionApproval,
    ExecutionArtifact,
    ExecutionArtifactType,
    ExecutionMode,
    ExecutionState,
    WorkbenchVendorBillExecutionResult,
    WorkbenchVendorBillExecutionStatus,
)
from app.application.quotation import (
    WorkbenchQuotationScenarioEvidenceResult,
    WorkbenchQuotationScenarioEvidenceStatus,
)
from app.application.workbench import (
    ReviewDecisionAcknowledgement,
    ReviewDecisionCommand,
    ReviewDecisionType,
    ReviewItem,
    ReviewQueueResult,
    ReviewStatus,
    WorkbenchDecisionIngestionCandidateResult,
    WorkbenchDecisionIngestionResult,
    WorkbenchDecisionIngestionStatus,
)
from app.application.workbench.exceptions import (
    ProductRemediationEligibilityError,
    ProductRemediationRaceError,
    ProductRemediationSupplierUnresolvedError,
    ReviewDecisionError,
    ReviewDecisionIdempotencyConflictError,
    ReviewNotFoundError,
    ReviewPersistenceError,
    ReviewQueryError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
    WorkbenchContractError,
)
from app.application.workbench.product_remediation import (
    CreateNewProductCommand,
    CreateNewProductResult,
    ProductRemediationStatus,
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
            "business_context_allocations": _allocation_payload(),
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
    assert command.business_context_allocations is not None
    assert command.business_context_allocations.allocations[0].sales_order_id == 301
    assert command.business_context_allocations.allocations[0].customer_invoice_id == 9001
    assert command.business_context_allocations.allocations[0].amount == Decimal("40000.000000")
    assert use_case.calls == 1


async def test_post_select_workflow_accepts_account_only_line_resolution(api_client: AsyncClient) -> None:
    use_case = FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.SELECT_WORKFLOW))

    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        submit_use_case=use_case,
        json={
            **_select_workflow_payload(),
            "line_resolutions": [{"line_number": "1", "account_only": True, "expense_account_id": 9001}],
        },
    )

    assert response.status_code == 200
    resolution = use_case.last_command.line_resolutions[0]
    assert resolution.account_only is True
    assert resolution.selected_product_id is None
    assert resolution.expense_account_id == 9001


async def test_post_select_workflow_accepts_mixed_line_resolutions(api_client: AsyncClient) -> None:
    use_case = FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.SELECT_WORKFLOW))

    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        submit_use_case=use_case,
        json={
            **_select_workflow_payload(),
            "line_resolutions": [
                {"line_number": "1", "selected_product_id": 800},
                {"line_number": "2", "account_only": True, "expense_account_id": 9001},
            ],
        },
    )

    assert response.status_code == 200
    resolutions = {r.line_number: r for r in use_case.last_command.line_resolutions}
    assert resolutions["1"].selected_product_id == 800
    assert resolutions["1"].account_only is False
    assert resolutions["1"].expense_account_id is None
    assert resolutions["2"].account_only is True
    assert resolutions["2"].selected_product_id is None
    assert resolutions["2"].expense_account_id == 9001


async def test_line_resolution_rejects_both_selected_product_id_and_account_only(api_client: AsyncClient) -> None:
    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        submit_use_case=FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.SELECT_WORKFLOW)),
        json={
            **_select_workflow_payload(),
            "line_resolutions": [{"line_number": "1", "selected_product_id": 800, "account_only": True}],
        },
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "request_validation_error"


async def test_line_resolution_rejects_neither_selected_product_id_nor_account_only(api_client: AsyncClient) -> None:
    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        submit_use_case=FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.SELECT_WORKFLOW)),
        json={
            **_select_workflow_payload(),
            "line_resolutions": [{"line_number": "1"}],
        },
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "request_validation_error"


async def test_line_resolution_rejects_explicit_account_only_false_without_product(api_client: AsyncClient) -> None:
    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        submit_use_case=FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.SELECT_WORKFLOW)),
        json={
            **_select_workflow_payload(),
            "line_resolutions": [{"line_number": "1", "account_only": False}],
        },
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "request_validation_error"


async def test_line_resolution_rejects_account_only_without_expense_account_id(api_client: AsyncClient) -> None:
    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        submit_use_case=FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.SELECT_WORKFLOW)),
        json={
            **_select_workflow_payload(),
            "line_resolutions": [{"line_number": "1", "account_only": True}],
        },
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "request_validation_error"


async def test_line_resolution_rejects_expense_account_id_without_account_only(api_client: AsyncClient) -> None:
    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        submit_use_case=FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.SELECT_WORKFLOW)),
        json={
            **_select_workflow_payload(),
            "line_resolutions": [{"line_number": "1", "selected_product_id": 800, "expense_account_id": 9001}],
        },
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "request_validation_error"


async def test_line_resolution_rejects_unknown_field(api_client: AsyncClient) -> None:
    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        submit_use_case=FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.SELECT_WORKFLOW)),
        json={
            **_select_workflow_payload(),
            "line_resolutions": [{"line_number": "1", "selected_product_id": 800, "post_as_expense": True}],
        },
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "request_validation_error"


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


async def test_legacy_business_context_field_is_rejected(api_client: AsyncClient) -> None:
    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        submit_use_case=FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.SELECT_WORKFLOW)),
        json={
            "expected_version": 1,
            "decision": "select_workflow",
            "selected_workflow": "vendor_bill",
            "business_context": {"sales_order_id": 1000},
            "idempotency_key": "key-1",
        },
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["message"] == "Unsupported Workbench decision field."


async def test_float_allocation_amount_is_rejected_safely(api_client: AsyncClient) -> None:
    payload = _select_workflow_payload()
    payload["business_context_allocations"] = _allocation_payload(amount=40000.0)

    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        submit_use_case=FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.SELECT_WORKFLOW)),
        json=payload,
    )

    assert response.status_code == 400
    assert response.json()["errors"][0] == {"code": "request_validation_error", "message": "Request validation failed."}


async def test_malformed_allocation_enum_is_rejected_safely(api_client: AsyncClient) -> None:
    payload = _select_workflow_payload()
    payload["business_context_allocations"] = _allocation_payload(allocation_type="not-real")

    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        submit_use_case=FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.SELECT_WORKFLOW)),
        json=payload,
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["message"] == "Request validation failed."


async def test_invalid_allocation_id_is_rejected_safely(api_client: AsyncClient) -> None:
    payload = _select_workflow_payload()
    payload["business_context_allocations"] = _allocation_payload(customer_invoice_id=0)

    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        submit_use_case=FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.SELECT_WORKFLOW)),
        json=payload,
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "workbench_contract_error"
    assert response.json()["errors"][0]["message"] == "customer_invoice_id must be a positive ERP id."


async def test_duplicate_allocation_key_is_rejected_safely(api_client: AsyncClient) -> None:
    payload = _select_workflow_payload()
    allocations = _allocation_payload()["allocations"]
    payload["business_context_allocations"] = {
        "completeness": "complete",
        "invoice_total": "80000.000000",
        "currency": "TRY",
        "allocations": [allocations[0], allocations[0]],
    }

    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        submit_use_case=FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.SELECT_WORKFLOW)),
        json=payload,
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["message"] == "allocation_key values must be unique."


async def test_complete_allocation_amount_mismatch_is_rejected_safely(api_client: AsyncClient) -> None:
    payload = _select_workflow_payload()
    payload["business_context_allocations"] = _allocation_payload(invoice_total="100.000000")

    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        submit_use_case=FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.SELECT_WORKFLOW)),
        json=payload,
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["message"] == "COMPLETE amount allocations must equal invoice_total."


async def test_complete_allocation_percentage_mismatch_is_rejected_safely(api_client: AsyncClient) -> None:
    payload = _select_workflow_payload()
    allocation_payload = _allocation_payload()
    allocation_payload["allocations"][0]["percentage"] = "30"
    payload["business_context_allocations"] = allocation_payload

    response = await _post_decision(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        submit_use_case=FakeSubmitUseCase(_acknowledgement(ReviewDecisionType.SELECT_WORKFLOW)),
        json=payload,
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["message"] == "COMPLETE percentage allocations must total 100."


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


# --------------------------------------------------------- P0-PROD-07H: product-resolution


def _product_resolution_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "mode": "create_new_product",
        "expected_version": 2,
        "line_number": "1",
        "product_name": "Yillik Aidat Urunu",
        "product_type": "service",
        "uom_id": 1,
    }
    body.update(overrides)
    return body


def _product_result(**overrides: Any) -> CreateNewProductResult:
    base = {
        "review_id": "review-1",
        "company_id": 7,
        "review_version": 2,
        "line_number": "1",
        "status": ProductRemediationStatus.COMPLETED,
        "product_template_id": 9001,
        "product_id": 9101,
        "supplierinfo_id": 9501,
        "created_product": True,
        "created_supplierinfo": True,
        "reused_existing_product": False,
        "already_applied": False,
        "safe_message": "Product created and linked to the supplier.",
    }
    base.update(overrides)
    return CreateNewProductResult(**base)


async def test_a_valid_request_delegates_to_07g_and_returns_product_result(api_client: AsyncClient) -> None:
    use_case = FakeCreateNewProductUseCase(_product_result())

    response = await _post_product_resolution(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE, company_id=7, user_id="finance.user"),
        use_case=use_case,
        json=_product_resolution_body(internal_reference="ICT-SKU-1", is_storable=True, note="approved"),
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["resolution_status"] == "completed"
    assert data["product_template_id"] == 9001
    assert data["product_id"] == 9101
    assert data["supplierinfo_id"] == 9501
    assert data["created_product"] is True
    assert data["needs_reconciliation"] is False

    command = use_case.last_command
    assert use_case.calls == 1
    assert command.review_id == "review-1"
    assert command.company_id == 7
    assert command.approved_by == "Finance User"  # user_name takes precedence, matching supplier-resolution
    assert command.expected_version == 2
    assert command.line_number == "1"
    assert command.product_name == "Yillik Aidat Urunu"
    assert command.product_type == "service"
    assert command.uom_id == 1
    assert command.internal_reference == "ICT-SKU-1"
    assert command.is_storable is True
    assert command.note == "approved"


async def test_bcd_client_cannot_supply_trusted_identity_fields(api_client: AsyncClient) -> None:
    for forbidden_field, value in (
        ("company_id", 999),
        ("seller_item_code", "SKU-100"),
        ("resolved_supplier_partner_id", 4010),
        ("product_template_id", 1),
        ("product_id", 1),
        ("supplierinfo_id", 1),
    ):
        response = await _post_product_resolution(
            api_client,
            "review-1",
            context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
            use_case=FakeCreateNewProductUseCase(_product_result()),
            json=_product_resolution_body(**{forbidden_field: value}),
        )
        assert response.status_code == 400, forbidden_field
        assert response.json()["errors"][0]["code"] == "request_validation_error"


async def test_e_expected_version_required(api_client: AsyncClient) -> None:
    body = _product_resolution_body()
    del body["expected_version"]
    response = await _post_product_resolution(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        use_case=FakeCreateNewProductUseCase(_product_result()),
        json=body,
    )
    assert response.status_code == 400


async def test_product_type_is_required_by_the_api(api_client: AsyncClient) -> None:
    body = _product_resolution_body()
    del body["product_type"]
    response = await _post_product_resolution(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        use_case=FakeCreateNewProductUseCase(_product_result()),
        json=body,
    )
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "request_validation_error"


async def test_product_type_has_no_default_and_only_accepts_consu_or_service(api_client: AsyncClient) -> None:
    for invalid_value in ("combo", "goods", "SERVICE", "", None):
        use_case = FakeCreateNewProductUseCase(_product_result())
        response = await _post_product_resolution(
            api_client,
            "review-1",
            context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
            use_case=use_case,
            json=_product_resolution_body(product_type=invalid_value),
        )
        assert response.status_code == 400, invalid_value
        assert use_case.calls == 0


async def test_product_type_consu_and_service_are_both_accepted_and_forwarded(api_client: AsyncClient) -> None:
    for value in ("consu", "service"):
        use_case = FakeCreateNewProductUseCase(_product_result())
        response = await _post_product_resolution(
            api_client,
            "review-1",
            context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
            use_case=use_case,
            json=_product_resolution_body(product_type=value),
        )
        assert response.status_code == 200, value
        assert use_case.last_command.product_type == value


async def test_f_uom_id_required_and_must_be_positive(api_client: AsyncClient) -> None:
    body_missing = _product_resolution_body()
    del body_missing["uom_id"]
    response_missing = await _post_product_resolution(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        use_case=FakeCreateNewProductUseCase(_product_result()),
        json=body_missing,
    )
    assert response_missing.status_code == 400

    use_case_negative = FakeCreateNewProductUseCase(_product_result())
    response_negative = await _post_product_resolution(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        use_case=use_case_negative,
        json=_product_resolution_body(uom_id=0),
    )
    # uom_id > 0 is enforced by CreateNewProductCommand's own __post_init__, which runs before the
    # use case is ever reached -- the fake must not be called.
    assert response_negative.status_code in (400, 409, 500)
    assert use_case_negative.calls == 0


async def test_g_supplier_unresolved_fails_closed_with_zero_writes(api_client: AsyncClient) -> None:
    use_case = FakeCreateNewProductUseCase(
        ProductRemediationSupplierUnresolvedError("No accepted supplier resolution exists for this review.")
    )
    response = await _post_product_resolution(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        use_case=use_case,
        json=_product_resolution_body(),
    )
    assert response.status_code == 409
    assert use_case.calls == 1


async def test_h_version_mismatch_fails_closed(api_client: AsyncClient) -> None:
    use_case = FakeCreateNewProductUseCase(
        ProductRemediationEligibilityError("The review version does not match expected_version.")
    )
    response = await _post_product_resolution(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        use_case=use_case,
        json=_product_resolution_body(),
    )
    assert response.status_code == 409


async def test_i_write_gate_disabled_maps_to_403_with_zero_odoo_writes(api_client: AsyncClient) -> None:
    use_case = FakeCreateNewProductUseCase(
        ProductWriteSafetyGateError("Product remediation master-data write must be explicitly enabled.")
    )
    response = await _post_product_resolution(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        use_case=use_case,
        json=_product_resolution_body(),
    )
    assert response.status_code == 403
    assert use_case.calls == 1


async def test_j_completed_result_maps_correctly(api_client: AsyncClient) -> None:
    response = await _post_product_resolution(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        use_case=FakeCreateNewProductUseCase(_product_result()),
        json=_product_resolution_body(),
    )
    data = response.json()["data"]
    assert data["resolution_status"] == "completed"
    assert data["needs_reconciliation"] is False


async def test_k_reused_existing_product_result_maps_correctly(api_client: AsyncClient) -> None:
    response = await _post_product_resolution(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        use_case=FakeCreateNewProductUseCase(
            _product_result(created_product=False, created_supplierinfo=False, reused_existing_product=True)
        ),
        json=_product_resolution_body(),
    )
    data = response.json()["data"]
    assert data["resolution_status"] == "completed"
    assert data["reused_existing_product"] is True
    assert data["created_product"] is False
    assert data["needs_reconciliation"] is False


async def test_l_needs_reconciliation_is_explicit_not_false_success(api_client: AsyncClient) -> None:
    response = await _post_product_resolution(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        use_case=FakeCreateNewProductUseCase(
            _product_result(
                status=ProductRemediationStatus.RECONCILIATION_REQUIRED,
                created_product=False,
                created_supplierinfo=False,
                already_applied=True,
                safe_message="A prior Odoo product creation attempt's outcome could not be verified.",
            )
        ),
        json=_product_resolution_body(),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["resolution_status"] == "reconciliation_required"
    assert data["needs_reconciliation"] is True


async def test_m_replay_returns_stable_result_and_causes_no_duplicate_write(api_client: AsyncClient) -> None:
    use_case = FakeCreateNewProductUseCase(_product_result(already_applied=True))
    first = await _post_product_resolution(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        use_case=use_case,
        json=_product_resolution_body(),
    )
    second = await _post_product_resolution(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        use_case=use_case,
        json=_product_resolution_body(),
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["data"] == second.json()["data"]
    # 07G's own persisted identities are what guarantee no duplicate Odoo write on replay;
    # the endpoint calls the use case exactly once per request either way.
    assert use_case.calls == 2


async def test_n_identity_conflict_or_in_flight_race_maps_to_409(api_client: AsyncClient) -> None:
    for exc in (ProductRemediationRaceError("Another request is currently creating a product for this identity."),):
        use_case = FakeCreateNewProductUseCase(exc)
        response = await _post_product_resolution(
            api_client,
            "review-1",
            context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
            use_case=use_case,
            json=_product_resolution_body(),
        )
        assert response.status_code == 409


async def test_o_wrong_company_fails_closed(api_client: AsyncClient) -> None:
    use_case = FakeCreateNewProductUseCase(ReviewNotFoundError("Review item was not found."))
    response = await _post_product_resolution(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE, company_id=999),
        use_case=use_case,
        json=_product_resolution_body(),
    )
    assert response.status_code == 404
    assert use_case.last_command.company_id == 999


def test_pqrs_no_auto_decision_pinning_or_execution_or_supplier_creation() -> None:
    """Structural: the endpoint's own dependencies are incapable of any of these actions.

    Checked via the function signature (not the full source, which legitimately
    mentions these terms in its own docstring explaining what it does NOT do).
    """
    import inspect

    from app.api.routers import workbench as workbench_router

    signature = inspect.signature(workbench_router.resolve_review_product)
    dependency_type_names = {str(param.annotation) for param in signature.parameters.values()}
    for forbidden in (
        "SubmitReviewDecisionUseCaseDep",
        "WorkbenchAcceptedDecisionExecutionDispatcherDep",
        "ResolveWorkbenchSupplierUseCaseDep",
    ):
        assert not any(forbidden in name for name in dependency_type_names)


def test_one_off_vendor_not_hub_owned_error_maps_to_409() -> None:
    """P0-PROD-08I: the ownership-protection failure must be an operator-visible
    conflict, never a 500 -- it is an expected business outcome (the exact-VAT match
    is a pre-existing partner the Hub never created via ONE_OFF_VENDOR), not a bug."""

    from app.api.routers import workbench as workbench_router
    from app.application.workbench.exceptions import SupplierResolutionOneOffVendorNotHubOwnedError

    status_code = workbench_router._status_code_for_exception(
        SupplierResolutionOneOffVendorNotHubOwnedError("not hub-owned")
    )

    assert status_code == 409


async def test_t_extra_request_fields_are_rejected(api_client: AsyncClient) -> None:
    response = await _post_product_resolution(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        use_case=FakeCreateNewProductUseCase(_product_result()),
        json=_product_resolution_body(unexpected_field="anything"),
    )
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "request_validation_error"


async def test_product_resolution_permission_required(api_client: AsyncClient) -> None:
    response = await _post_product_resolution(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_READ),
        use_case=FakeCreateNewProductUseCase(_product_result()),
        json=_product_resolution_body(),
    )
    assert response.status_code == 403


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


async def test_explicit_odoo_workbench_decision_ingestion_trigger(api_client: AsyncClient) -> None:
    workflow = FakeDecisionIngestionWorkflow(
        WorkbenchDecisionIngestionResult(
            company_id=7,
            processed_count=1,
            already_processed_count=0,
            acknowledgement_failed_count=0,
            failed_count=0,
            results=(
                WorkbenchDecisionIngestionCandidateResult(
                    review_id="review-1",
                    odoo_record_id=42,
                    status=WorkbenchDecisionIngestionStatus.PROCESSED,
                    acknowledged=True,
                    idempotency_key="odoo-workbench-decision:abc",
                ),
            ),
        )
    )

    response = await _post_decision_sync(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE, company_id=7),
        workflow=workflow,
        params={"limit": "25"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["processed_count"] == 1
    assert body["data"]["results"][0]["status"] == "processed"
    assert body["data"]["results"][0]["acknowledged"] is True
    assert workflow.calls == [{"company_id": 7, "limit": 25, "trace_id": "trace-123"}]


async def test_decide_permission_required_for_decision_ingestion_trigger(api_client: AsyncClient) -> None:
    response = await _post_decision_sync(api_client, context=_context(Permission.WORKBENCH_REVIEW_READ))

    assert response.status_code == 403
    assert response.json()["errors"][0]["code"] == "permission_denied"


async def test_explicit_workbench_vendor_bill_execution_trigger(api_client: AsyncClient) -> None:
    workflow = FakeWorkbenchVendorBillExecutionWorkflow(
        WorkbenchVendorBillExecutionResult(
            review_id="review-from-path",
            company_id=7,
            decision_version=3,
            mode=ExecutionMode.DRY_RUN,
            status=WorkbenchVendorBillExecutionStatus.DRY_RUN_COMPLETED,
            execution_id="execution-1",
            runtime_state=ExecutionState.COMPLETED,
            artifacts=(
                ExecutionArtifact(
                    artifact_type=ExecutionArtifactType.VENDOR_BILL,
                    artifact_id="9001",
                    external_identity="vendor-bill-write:key",
                    created=False,
                ),
            ),
        )
    )

    response = await _post_execute(
        api_client,
        "review-from-path",
        context=_context(Permission.WORKBENCH_EXECUTE, company_id=7),
        workflow=workflow,
        json={"decision_version": 3, "mode": "dry_run"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["status"] == "dry_run_completed"
    assert body["data"]["runtime_state"] == "completed"
    assert body["data"]["artifacts"] == [
        {
            "artifact_type": "vendor_bill",
            "artifact_id": "9001",
            "external_identity": "vendor-bill-write:key",
            "created": False,
        }
    ]
    assert workflow.calls == [
        {
            "review_id": "review-from-path",
            "company_id": 7,
            "decision_version": 3,
            "mode": ExecutionMode.DRY_RUN,
            "approval": None,
            "trace_id": "trace-123",
        }
    ]


async def test_explicit_workbench_quotation_scenario_evidence_trigger(api_client: AsyncClient) -> None:
    workflow = FakeWorkbenchQuotationScenarioEvidenceWorkflow(
        WorkbenchQuotationScenarioEvidenceResult(
            review_id="review-from-path",
            company_id=7,
            decision_version=4,
            status=WorkbenchQuotationScenarioEvidenceStatus.CAPTURED,
            decision_id="decision-xyz",
            persisted_scenario_ids=("scenario-a", "scenario-b"),
        )
    )

    response = await _post_quotation_scenarios(
        api_client,
        "review-from-path",
        context=_context(Permission.WORKBENCH_EXECUTE, company_id=7),
        workflow=workflow,
        json={"decision_version": 4},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["status"] == "captured"
    assert body["data"]["persisted_scenario_ids"] == ["scenario-a", "scenario-b"]
    assert body["data"]["decision_id"] == "decision-xyz"
    assert workflow.calls == [
        {
            "review_id": "review-from-path",
            "company_id": 7,
            "decision_version": 4,
            "trace_id": "trace-123",
        }
    ]


async def test_workbench_quotation_scenarios_permission_required(api_client: AsyncClient) -> None:
    workflow = FakeWorkbenchQuotationScenarioEvidenceWorkflow(AssertionError("capture must not run"))

    response = await _post_quotation_scenarios(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_READ),
        workflow=workflow,
        json={"decision_version": 4},
    )

    assert response.status_code == 403
    assert response.json()["errors"][0]["code"] == "permission_denied"
    assert workflow.calls == []


async def test_workbench_quotation_scenarios_body_cannot_supply_scenario_ids(api_client: AsyncClient) -> None:
    response = await _post_quotation_scenarios(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_EXECUTE),
        json={"decision_version": 4, "selected_quotation_scenario_ids": ["scenario-a"]},
    )

    assert response.status_code == 400


async def test_workbench_vendor_bill_execute_reuses_execution_approval(api_client: AsyncClient) -> None:
    workflow = FakeWorkbenchVendorBillExecutionWorkflow(
        WorkbenchVendorBillExecutionResult(
            review_id="review-1",
            company_id=7,
            decision_version=3,
            mode=ExecutionMode.EXECUTE,
            status=WorkbenchVendorBillExecutionStatus.EXECUTED,
            execution_id="execution-1",
            runtime_state=ExecutionState.COMPLETED,
        )
    )

    response = await _post_execute(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_EXECUTE, company_id=7),
        workflow=workflow,
        json={"decision_version": 3, "mode": "execute", "approval": {"approved_by": "controller"}},
    )

    assert response.status_code == 200
    approval = workflow.calls[0]["approval"]
    assert isinstance(approval, ExecutionApproval)
    assert approval.approved_by == "controller"


async def test_workbench_execute_permission_required_before_runtime_execution(api_client: AsyncClient) -> None:
    workflow = FakeWorkbenchVendorBillExecutionWorkflow(AssertionError("execution must not run"))

    response = await _post_execute(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        workflow=workflow,
        json={"decision_version": 3, "mode": "dry_run"},
    )

    assert response.status_code == 403
    assert response.json()["errors"][0]["code"] == "permission_denied"
    assert workflow.calls == []


async def test_workbench_execute_body_cannot_supply_erp_payload(api_client: AsyncClient) -> None:
    response = await _post_execute(
        api_client,
        "review-1",
        context=_context(Permission.WORKBENCH_EXECUTE),
        json={"decision_version": 3, "mode": "dry_run", "invoice_lines": []},
    )

    assert response.status_code == 400
    assert response.json()["errors"][0] == {"code": "request_validation_error", "message": "Request validation failed."}


async def test_workbench_execute_route_reaches_customer_quotation_dispatch(api_client: AsyncClient) -> None:
    dispatcher = FakeWorkbenchVendorBillExecutionWorkflow(
        WorkbenchVendorBillExecutionResult(
            review_id="review-quote",
            company_id=7,
            decision_version=4,
            mode=ExecutionMode.EXECUTE,
            status=WorkbenchVendorBillExecutionStatus.EXECUTED,
            execution_id="execution-1",
        )
    )

    response = await _post_execute(
        api_client,
        "review-quote",
        context=_context(Permission.WORKBENCH_EXECUTE, company_id=7),
        workflow=dispatcher,
        json={"decision_version": 4, "mode": "execute", "approval": {"approved_by": "controller"}},
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "executed"
    assert dispatcher.calls == [
        {
            "review_id": "review-quote",
            "company_id": 7,
            "decision_version": 4,
            "mode": ExecutionMode.EXECUTE,
            "approval": ExecutionApproval(approved_by="controller"),
            "trace_id": "trace-123",
        }
    ]


async def test_workbench_execute_route_surfaces_missing_quotation_evidence_block(api_client: AsyncClient) -> None:
    dispatcher = FakeWorkbenchVendorBillExecutionWorkflow(
        WorkbenchVendorBillExecutionResult(
            review_id="review-quote",
            company_id=7,
            decision_version=4,
            mode=ExecutionMode.EXECUTE,
            status=WorkbenchVendorBillExecutionStatus.MISSING_QUOTATION_EVIDENCE,
            message="Immutable quotation scenario evidence is missing for 1 selected scenario(s).",
        )
    )

    response = await _post_execute(
        api_client,
        "review-quote",
        context=_context(Permission.WORKBENCH_EXECUTE, company_id=7),
        workflow=dispatcher,
        json={"decision_version": 4, "mode": "execute", "approval": {"approved_by": "controller"}},
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "missing_quotation_evidence"


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


async def test_openapi_contains_expected_workbench_routes_and_no_identity_inputs(api_client: AsyncClient) -> None:
    response = await api_client.get("/openapi.json")

    paths = response.json()["paths"]
    workbench_paths = {path: methods for path, methods in paths.items() if path.startswith("/api/workbench")}
    assert set(workbench_paths) == {
        "/api/workbench/decisions/sync",
        "/api/workbench/reviews",
        "/api/workbench/reviews/{review_id}",
        "/api/workbench/reviews/{review_id}/decision",
        "/api/workbench/reviews/{review_id}/execute",
        "/api/workbench/reviews/{review_id}/product-resolution",
        "/api/workbench/reviews/{review_id}/quotation-scenarios",
        "/api/workbench/reviews/{review_id}/supplier-resolution",
    }
    product_resolution_schema = response.json()["components"]["schemas"]["ProductResolutionRequest"]
    product_resolution_text = str(product_resolution_schema)
    for forbidden in (
        "company_id",
        "review_id",
        "approved_by",
        "resolved_supplier_partner_id",
        "seller_item_code",
        "product_template_id",
        '"product_id"',
        "supplierinfo_id",
    ):
        assert forbidden not in product_resolution_text
    assert set(product_resolution_schema["properties"]) == {
        "mode",
        "expected_version",
        "line_number",
        "product_name",
        "product_type",
        "uom_id",
        "internal_reference",
        "is_storable",
        "note",
    }
    assert product_resolution_schema.get("additionalProperties") is False
    assert "product_type" in product_resolution_schema.get("required", [])
    assert product_resolution_schema["properties"]["product_type"]["enum"] == ["consu", "service"]
    supplier_resolution_schema = response.json()["components"]["schemas"]["SupplierResolutionRequest"]
    supplier_resolution_text = str(supplier_resolution_schema)
    for forbidden in (
        "company_id",
        "review_id",
        "approved_by",
        "supplier_name",
        "supplier_vat",
        "supplier_tax_number",
        "invoice_number",
        "ettn",
    ):
        assert forbidden not in supplier_resolution_text
    assert set(supplier_resolution_schema["properties"]) == {"mode", "expected_version", "partner_id", "note"}
    assert supplier_resolution_schema.get("additionalProperties") is False
    mode_ref = supplier_resolution_schema["properties"]["mode"]["$ref"].split("/")[-1]
    assert "one_off_vendor" in response.json()["components"]["schemas"][mode_ref]["enum"]
    assert "use_one_off_supplier" in response.json()["components"]["schemas"][mode_ref]["enum"]
    supplier_remediation_schema = response.json()["components"]["schemas"]["SupplierRemediationResponse"]
    for expected in (
        "one_off_vendor_hub_owned",
        "one_off_vendor_retirement_status",
        "one_off_vendor_awaiting_vendor_bill",
        "one_off_vendor_reconciliation_required",
    ):
        assert expected in supplier_remediation_schema["properties"]
    decision_schema = response.json()["components"]["schemas"]["ReviewDecisionRequest"]
    schema_text = str(decision_schema)
    assert "company_id" not in schema_text
    assert "decided_by" not in schema_text
    assert "review_id" not in schema_text
    assert "business_context_allocations" in decision_schema["properties"]
    assert "business_context" not in decision_schema["properties"]
    assert "BusinessContextAllocationRequest" in response.json()["components"]["schemas"]
    assert "BusinessContextAllocationSetRequest" in response.json()["components"]["schemas"]
    execution_schema = response.json()["components"]["schemas"]["WorkbenchVendorBillExecutionRequest"]
    execution_schema_text = str(execution_schema)
    for field in ("vendor", "invoice_lines", "tax", "product", "amount", "currency", "purchase_order"):
        assert field not in execution_schema_text
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


class FakeCreateNewProductUseCase:
    def __init__(self, result: CreateNewProductResult | Exception) -> None:
        self.result = result
        self.calls = 0
        self.last_command: CreateNewProductCommand | None = None

    async def execute(self, command: CreateNewProductCommand) -> CreateNewProductResult:
        self.calls += 1
        self.last_command = command
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeDecisionIngestionWorkflow:
    def __init__(self, result: WorkbenchDecisionIngestionResult | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def sync_ready_decisions(
        self,
        *,
        company_id: int,
        limit: int,
        trace_id: str | None = None,
    ) -> WorkbenchDecisionIngestionResult:
        self.calls.append({"company_id": company_id, "limit": limit, "trace_id": trace_id})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeWorkbenchVendorBillExecutionWorkflow:
    def __init__(self, result: WorkbenchVendorBillExecutionResult | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def execute(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
        mode: ExecutionMode,
        approval: ExecutionApproval | None,
        trace_id: str | None = None,
    ) -> WorkbenchVendorBillExecutionResult:
        self.calls.append(
            {
                "review_id": review_id,
                "company_id": company_id,
                "decision_version": decision_version,
                "mode": mode,
                "approval": approval,
                "trace_id": trace_id,
            }
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeWorkbenchQuotationScenarioEvidenceWorkflow:
    def __init__(self, result: WorkbenchQuotationScenarioEvidenceResult | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def capture(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
        trace_id: str | None = None,
    ) -> WorkbenchQuotationScenarioEvidenceResult:
        self.calls.append(
            {
                "review_id": review_id,
                "company_id": company_id,
                "decision_version": decision_version,
                "trace_id": trace_id,
            }
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def _post_quotation_scenarios(
    api_client: AsyncClient,
    review_id: str,
    *,
    context: RequestContext,
    json: dict[str, Any],
    workflow: FakeWorkbenchQuotationScenarioEvidenceWorkflow | None = None,
):
    app.dependency_overrides[get_request_context] = lambda: context
    if workflow is not None:
        app.dependency_overrides[get_workbench_quotation_scenario_evidence_workflow] = lambda: workflow
    try:
        return await api_client.post(f"/api/workbench/reviews/{review_id}/quotation-scenarios", json=json)
    finally:
        app.dependency_overrides.clear()


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


async def _post_product_resolution(
    api_client: AsyncClient,
    review_id: str,
    *,
    context: RequestContext,
    json: dict[str, Any],
    use_case: FakeCreateNewProductUseCase | None = None,
):
    app.dependency_overrides[get_request_context] = lambda: context
    if use_case is not None:
        app.dependency_overrides[get_create_new_product_use_case] = lambda: use_case
    try:
        return await api_client.post(f"/api/workbench/reviews/{review_id}/product-resolution", json=json)
    finally:
        app.dependency_overrides.clear()


async def _post_decision_sync(
    api_client: AsyncClient,
    *,
    context: RequestContext,
    workflow: FakeDecisionIngestionWorkflow | None = None,
    params: dict[str, str] | None = None,
):
    app.dependency_overrides[get_request_context] = lambda: context
    if workflow is not None:
        app.dependency_overrides[get_workbench_decision_ingestion_workflow] = lambda: workflow
    try:
        return await api_client.post("/api/workbench/decisions/sync", params=params)
    finally:
        app.dependency_overrides.clear()


async def _post_execute(
    api_client: AsyncClient,
    review_id: str,
    *,
    context: RequestContext,
    json: dict[str, Any],
    workflow: FakeWorkbenchVendorBillExecutionWorkflow | None = None,
):
    app.dependency_overrides[get_request_context] = lambda: context
    if workflow is not None:
        app.dependency_overrides[get_workbench_accepted_decision_execution_dispatcher] = lambda: workflow
    try:
        return await api_client.post(f"/api/workbench/reviews/{review_id}/execute", json=json)
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


def _select_workflow_payload() -> dict[str, Any]:
    return {
        "expected_version": 1,
        "decision": "select_workflow",
        "selected_workflow": "vendor_bill",
        "business_context_allocations": _allocation_payload(),
        "idempotency_key": "key-1",
    }


def _allocation_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "completeness": "complete",
        "invoice_total": "100000.000000",
        "currency": "TRY",
        "allocations": [
            {
                "allocation_key": "ALLOC-001",
                "allocation_type": "sales_order_cost",
                "source_line_number": "1",
                "description": "Customer A share",
                "amount": "40000.000000",
                "percentage": "40",
                "currency": "TRY",
                "customer_id": 101,
                "recharge_partner_id": 105,
                "customer_invoice_id": 9001,
                "sales_order_id": 301,
            },
            {
                "allocation_key": "ALLOC-002",
                "allocation_type": "internal_cost",
                "amount": "60000.000000",
                "percentage": "60",
                "currency": "TRY",
            },
        ],
    }
    if "amount" in overrides:
        payload["allocations"][0]["amount"] = overrides.pop("amount")
    if "allocation_type" in overrides:
        payload["allocations"][0]["allocation_type"] = overrides.pop("allocation_type")
    if "customer_invoice_id" in overrides:
        payload["allocations"][0]["customer_invoice_id"] = overrides.pop("customer_invoice_id")
    payload.update(overrides)
    return payload
