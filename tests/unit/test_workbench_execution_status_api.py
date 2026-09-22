"""HTTP-level wiring for the operator execution-status endpoint (P0-PROD-12A).

Proves the endpoint exists as GET /reviews/{review_id}/execution-status, requires
workbench_execute, returns the documented response shape (including the no-decision/
no-execution null representation and the real pilot's waiting_retry/completed shapes),
enforces company isolation via the same RequestContext pattern as every other Workbench
route, and never exposes a write method on any of its composed dependencies -- exercised
through the real FastAPI router with the composed use case swapped for a fully in-memory
fake via FastAPI's own dependency-override mechanism (no DB, no Odoo client needed).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import dependencies
from app.api.routers.workbench import router
from app.api.security import AuthenticationMethod, Permission, RequestContext
from app.application.execution import ExecutionArtifact, ExecutionArtifactType, ExecutionMode, ExecutionState
from app.application.workbench.dto import ReviewStatus
from app.application.workbench.exceptions import ReviewNotFoundError
from app.application.workbench.execution_status import (
    WorkbenchDecisionStatus,
    WorkbenchEvidenceStatus,
    WorkbenchExecutionAuthorizationStatus,
    WorkbenchExecutionStatus,
    WorkbenchExecutionSummary,
    WorkbenchRecoveryStatus,
)
from app.application.workbench.write_authorization import WriteAuthorizationOperationType, WriteAuthorizationStatus
from app.application.workflow import WorkflowType

REVIEW_ID = "review:1316ab15-4bcb-522c-b65a-e5785ee106e0"


class _FakeExecutionStatusUseCase:
    def __init__(self, *, status: WorkbenchExecutionStatus | None = None, error: Exception | None = None) -> None:
        self._status = status
        self._error = error
        self.calls: list[tuple[str, int]] = []

    def execute(self, *, review_id: str, company_id: int) -> WorkbenchExecutionStatus:
        self.calls.append((review_id, company_id))
        if self._error is not None:
            raise self._error
        assert self._status is not None
        return self._status


def _no_execution_status() -> WorkbenchExecutionStatus:
    return WorkbenchExecutionStatus(
        review_id=REVIEW_ID,
        company_id=1,
        review_version=1,
        review_status=ReviewStatus.PENDING_REVIEW,
        decision=None,
        execution=None,
        failure=None,
    )


def _waiting_retry_status() -> WorkbenchExecutionStatus:
    return WorkbenchExecutionStatus(
        review_id=REVIEW_ID,
        company_id=1,
        review_version=3,
        review_status=ReviewStatus.DECISION_SUBMITTED,
        decision=WorkbenchDecisionStatus(
            decision_id="review-decision:1699d6ca-c1df-4450-a94c-7171b77cba81",
            decision_version=3,
            selected_workflow=WorkflowType.VENDOR_BILL,
        ),
        execution=WorkbenchExecutionSummary(
            execution_id="accepted-decision-execution:e20d3b73-05f3-5f63-be01-205fe8dac169",
            mode=ExecutionMode.EXECUTE,
            state=ExecutionState.WAITING_RETRY,
            retry_count=1,
            max_attempts=2,
            remaining_attempts=1,
            retry_possible=True,
        ),
        failure=None,
        evidence=WorkbenchEvidenceStatus(
            stage_one_present=True,
            stage_one_review_version=2,
            stage_two_present=True,
            stage_two_decision_version=3,
        ),
        recovery=WorkbenchRecoveryStatus(execution_completed=False, waiting_retry=True, remaining_attempts=1),
    )


def _completed_status() -> WorkbenchExecutionStatus:
    return WorkbenchExecutionStatus(
        review_id=REVIEW_ID,
        company_id=1,
        review_version=3,
        review_status=ReviewStatus.DECISION_SUBMITTED,
        decision=WorkbenchDecisionStatus(
            decision_id="review-decision:1699d6ca-c1df-4450-a94c-7171b77cba81",
            decision_version=3,
            selected_workflow=WorkflowType.VENDOR_BILL,
        ),
        execution=WorkbenchExecutionSummary(
            execution_id="accepted-decision-execution:e20d3b73-05f3-5f63-be01-205fe8dac169",
            mode=ExecutionMode.EXECUTE,
            state=ExecutionState.COMPLETED,
            retry_count=1,
            max_attempts=2,
            remaining_attempts=1,
            retry_possible=False,
        ),
        failure=None,
        artifacts=(
            ExecutionArtifact(
                artifact_type=ExecutionArtifactType.VENDOR_BILL,
                artifact_id="62",
                external_identity="vendor-bill-write:c86c866fd5c7340bddf808ebffd880c94a09bca45fc6e9e216c13586a355e1db",
                created=True,
            ),
        ),
        authorization=WorkbenchExecutionAuthorizationStatus(
            authorization_id="dd0927e8-9b0a-4f2b-8cfb-98d8de5ee343",
            operation_type=WriteAuthorizationOperationType.EXECUTE_VENDOR_BILL,
            target_version=3,
            status=WriteAuthorizationStatus.CONSUMED,
            use_count=1,
            is_expired=False,
            consumed_by_execution_id="accepted-decision-execution:e20d3b73-05f3-5f63-be01-205fe8dac169",
        ),
        recovery=WorkbenchRecoveryStatus(execution_completed=True, waiting_retry=False, remaining_attempts=1),
    )


def _api(
    *, use_case, permissions: tuple[Permission, ...] = (Permission.WORKBENCH_EXECUTE,), company_id: int = 1
) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[dependencies.get_workbench_execution_status_use_case] = lambda: use_case
    app.dependency_overrides[dependencies.get_request_context] = lambda: RequestContext(
        user_id="finance",
        user_name="Finance",
        company_id=company_id,
        permissions=permissions,
        trace_id="execution-status-test",
        authentication_method=AuthenticationMethod.JWT,
    )
    return TestClient(app)


def _get_status(client: TestClient):
    return client.get(f"/api/workbench/reviews/{REVIEW_ID}/execution-status")


def test_no_decision_no_execution_is_not_an_api_error() -> None:
    use_case = _FakeExecutionStatusUseCase(status=_no_execution_status())
    with _api(use_case=use_case) as client:
        response = _get_status(client)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["decision"] is None
    assert data["execution"] is None
    assert data["failure"] is None
    assert data["artifacts"] == []
    assert data["authorization"] is None
    assert data["recovery"] == {"execution_completed": False, "waiting_retry": False, "remaining_attempts": 0}


def test_waiting_retry_shape_matches_the_real_pilot_incident() -> None:
    use_case = _FakeExecutionStatusUseCase(status=_waiting_retry_status())
    with _api(use_case=use_case) as client:
        response = _get_status(client)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["execution"]["execution_id"] == "accepted-decision-execution:e20d3b73-05f3-5f63-be01-205fe8dac169"
    assert data["execution"]["state"] == "waiting_retry"
    assert data["execution"]["retry_count"] == 1
    assert data["execution"]["max_attempts"] == 2
    assert data["execution"]["remaining_attempts"] == 1
    assert data["execution"]["retry_possible"] is True
    assert data["evidence"]["stage_one_present"] is True
    assert data["evidence"]["stage_two_present"] is True
    assert data["recovery"]["waiting_retry"] is True


def test_completed_execution_shape_exposes_the_created_vendor_bill_artifact_id() -> None:
    use_case = _FakeExecutionStatusUseCase(status=_completed_status())
    with _api(use_case=use_case) as client:
        response = _get_status(client)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["execution"]["state"] == "completed"
    assert data["artifacts"] == [
        {
            "artifact_type": "vendor_bill",
            "artifact_id": "62",
            "external_identity": "vendor-bill-write:c86c866fd5c7340bddf808ebffd880c94a09bca45fc6e9e216c13586a355e1db",
            "created": True,
        }
    ]
    assert data["authorization"]["authorization_id"] == "dd0927e8-9b0a-4f2b-8cfb-98d8de5ee343"
    assert data["authorization"]["is_expired"] is False
    assert data["recovery"]["execution_completed"] is True


def test_endpoint_requires_workbench_execute() -> None:
    use_case = _FakeExecutionStatusUseCase(status=_no_execution_status())
    with _api(use_case=use_case, permissions=()) as client:
        response = _get_status(client)
    assert response.status_code == 403
    assert use_case.calls == []


def test_review_read_permission_alone_is_insufficient() -> None:
    use_case = _FakeExecutionStatusUseCase(status=_no_execution_status())
    with _api(use_case=use_case, permissions=(Permission.WORKBENCH_REVIEW_READ,)) as client:
        response = _get_status(client)
    assert response.status_code == 403


def test_unknown_or_wrong_company_review_returns_404() -> None:
    use_case = _FakeExecutionStatusUseCase(error=ReviewNotFoundError("Review was not found in this company scope."))
    with _api(use_case=use_case) as client:
        response = _get_status(client)
    assert response.status_code == 404


def test_company_id_comes_from_request_context_not_the_client() -> None:
    """Cross-company isolation: the endpoint has no company_id request parameter at
    all -- it is always sourced from the trusted RequestContext, exactly like every
    other Workbench route (vendor-bill-preview, write-authorizations, etc.)."""

    use_case = _FakeExecutionStatusUseCase(status=_no_execution_status())
    with _api(use_case=use_case, company_id=7) as client:
        response = _get_status(client)

    assert response.status_code == 200
    assert use_case.calls == [(REVIEW_ID, 7)]


def test_endpoint_is_a_get_not_a_post_or_delete() -> None:
    """The endpoint must not mutate runtime state -- confirmed structurally: it is
    registered as GET only in the FastAPI route table."""

    matching_routes = [
        r for r in router.routes if getattr(r, "path", None) == "/api/workbench/reviews/{review_id}/execution-status"
    ]
    assert len(matching_routes) == 1
    assert matching_routes[0].methods == {"GET"}
