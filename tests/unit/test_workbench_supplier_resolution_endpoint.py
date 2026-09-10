"""POST /api/workbench/reviews/{review_id}/supplier-resolution (P0-3D2D).

The endpoint records an operator's supplier-resolution choice and triggers
reclassification. Legal identity never comes from the body; the authenticated
actor and company come from the security context.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.api.dependencies import get_request_context, get_resolve_workbench_supplier_use_case
from app.api.security import AuthenticationMethod, Permission, RequestContext
from app.application.exceptions.supplier_partner import SupplierPartnerWriteSafetyGateError
from app.application.workbench.exceptions import (
    ReviewNotFoundError,
    ReviewVersionConflictError,
    SupplierResolutionConflictError,
    SupplierResolutionPartnerMismatchError,
)
from app.application.workbench.supplier_remediation import (
    ResolveWorkbenchSupplierCommand,
    SupplierPartnerWriteEffectStatus,
    SupplierRemediationResult,
    SupplierRemediationStatus,
)
from app.application.workbench.supplier_resolution import SupplierResolutionMode
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.main import app

REVIEW_ID = "review:endpoint-1"


def _context(*permissions: Permission, company_id: int = 7, user_id: str = "op-1", user_name: str | None = "Ops One"):
    return RequestContext(
        user_id=user_id,
        user_name=user_name,
        company_id=company_id,
        permissions=permissions,
        trace_id="trace-xyz",
        authentication_method=AuthenticationMethod.JWT,
    )


class _FakeResolveUseCase:
    def __init__(self, *, result: SupplierRemediationResult | None = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.commands: list[ResolveWorkbenchSupplierCommand] = []

    async def execute(self, command: ResolveWorkbenchSupplierCommand) -> SupplierRemediationResult:
        self.commands.append(command)
        if self._error is not None:
            raise self._error
        return self._result  # type: ignore[return-value]


def _result(**kw: Any) -> SupplierRemediationResult:
    base: dict[str, Any] = {
        "review_id": REVIEW_ID,
        "company_id": 7,
        "mode": SupplierResolutionMode.MATCH_EXISTING,
        "status": SupplierRemediationStatus.RESOLVED,
        "previous_version": 1,
        "current_version": 2,
        "current_workflow": WorkflowType.VENDOR_BILL,
        "current_review_reasons": (),
        "effective_partner_id": 4010,
        "partner_write_status": SupplierPartnerWriteEffectStatus.SELECTED,
        "reclassified": True,
        "already_applied": False,
        "workbench_republished": False,
        "safe_message": "Supplier resolved; the review was reclassified.",
    }
    base.update(kw)
    return SupplierRemediationResult(**base)


async def _post(
    api_client: AsyncClient,
    *,
    context: RequestContext,
    json: dict[str, Any],
    use_case: _FakeResolveUseCase | None = None,
):
    app.dependency_overrides[get_request_context] = lambda: context
    if use_case is not None:
        app.dependency_overrides[get_resolve_workbench_supplier_use_case] = lambda: use_case
    try:
        return await api_client.post(f"/api/workbench/reviews/{REVIEW_ID}/supplier-resolution", json=json)
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------- happy paths


async def test_match_existing_happy_path(api_client: AsyncClient) -> None:
    use_case = _FakeResolveUseCase(result=_result())
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE, company_id=7),
        json={"mode": "match_existing", "expected_version": 1, "partner_id": 4010},
        use_case=use_case,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    assert data["review_id"] == REVIEW_ID
    assert data["mode"] == "match_existing"
    assert data["resolution_status"] == "resolved"
    assert data["current_version"] == 2
    assert data["partner_id"] == 4010
    assert data["partner_write_status"] == "selected"
    assert data["reclassified"] is True
    assert data["workbench_republished"] is False

    command = use_case.commands[0]
    assert command.review_id == REVIEW_ID
    assert command.company_id == 7  # from context, not body
    assert command.approved_by == "Ops One"  # authenticated actor
    assert command.mode is SupplierResolutionMode.MATCH_EXISTING
    assert command.resolved_partner_id == 4010


async def test_create_permanent_happy_path(api_client: AsyncClient) -> None:
    use_case = _FakeResolveUseCase(
        result=_result(
            mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER,
            partner_write_status=SupplierPartnerWriteEffectStatus.CREATED,
            effective_partner_id=6001,
        )
    )
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={"mode": "create_permanent_supplier", "expected_version": 1},
        use_case=use_case,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["mode"] == "create_permanent_supplier"
    assert data["partner_write_status"] == "created"
    assert data["partner_id"] == 6001
    assert use_case.commands[0].resolved_partner_id is None


async def test_one_off_returns_not_supported(api_client: AsyncClient) -> None:
    reason = ManualReviewReason(
        code=ManualReviewReasonCode.SUPPLIER_NOT_FOUND, message="x", source="partner_matching", candidate_count=0
    )
    use_case = _FakeResolveUseCase(
        result=_result(
            mode=SupplierResolutionMode.USE_ONE_OFF_SUPPLIER,
            status=SupplierRemediationStatus.ONE_OFF_EXECUTION_NOT_SUPPORTED,
            previous_version=1,
            current_version=1,
            current_workflow=WorkflowType.MANUAL_REVIEW,
            current_review_reasons=(reason,),
            effective_partner_id=None,
            partner_write_status=None,
            reclassified=False,
        )
    )
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={"mode": "use_one_off_supplier", "expected_version": 1},
        use_case=use_case,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["resolution_status"] == "one_off_execution_not_supported"
    assert data["partner_id"] is None
    assert data["current_version"] == 1
    assert data["current_review_reasons"][0]["code"] == "SUPPLIER_NOT_FOUND"


async def test_idempotent_retry_reports_already_applied(api_client: AsyncClient) -> None:
    use_case = _FakeResolveUseCase(result=_result(already_applied=True))
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={"mode": "match_existing", "expected_version": 1, "partner_id": 4010},
        use_case=use_case,
    )
    assert response.status_code == 200
    assert response.json()["data"]["already_applied"] is True


# --------------------------------------------------------- error mapping


async def test_missing_review_maps_to_404(api_client: AsyncClient) -> None:
    use_case = _FakeResolveUseCase(error=ReviewNotFoundError("Review item was not found."))
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={"mode": "match_existing", "expected_version": 1, "partner_id": 4010},
        use_case=use_case,
    )
    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["errors"][0]["code"] == "review_not_found"
    assert body["trace_id"] == "trace-xyz"


async def test_stale_version_maps_to_409(api_client: AsyncClient) -> None:
    use_case = _FakeResolveUseCase(error=ReviewVersionConflictError("stale"))
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={"mode": "match_existing", "expected_version": 1, "partner_id": 4010},
        use_case=use_case,
    )
    assert response.status_code == 409


async def test_partner_vat_mismatch_maps_to_409(api_client: AsyncClient) -> None:
    use_case = _FakeResolveUseCase(error=SupplierResolutionPartnerMismatchError("vat mismatch"))
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={"mode": "match_existing", "expected_version": 1, "partner_id": 4010},
        use_case=use_case,
    )
    assert response.status_code == 409


async def test_conflicting_resolution_maps_to_409(api_client: AsyncClient) -> None:
    use_case = _FakeResolveUseCase(error=SupplierResolutionConflictError("already decided"))
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={"mode": "create_permanent_supplier", "expected_version": 1},
        use_case=use_case,
    )
    assert response.status_code == 409


async def test_write_gate_failure_maps_to_403(api_client: AsyncClient) -> None:
    use_case = _FakeResolveUseCase(
        error=SupplierPartnerWriteSafetyGateError("Supplier remediation write must be explicitly enabled.")
    )
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={"mode": "create_permanent_supplier", "expected_version": 1},
        use_case=use_case,
    )
    assert response.status_code == 403
    assert "secret" not in str(response.json()).lower()
    assert "traceback" not in str(response.json()).lower()


# --------------------------------------------------------- contract / auth


async def test_permission_required(api_client: AsyncClient) -> None:
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_READ),
        json={"mode": "match_existing", "expected_version": 1, "partner_id": 4010},
        use_case=_FakeResolveUseCase(result=_result()),
    )
    assert response.status_code == 403


async def test_invalid_mode_is_rejected(api_client: AsyncClient) -> None:
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={"mode": "make_it_up", "expected_version": 1},
        use_case=_FakeResolveUseCase(result=_result()),
    )
    assert response.status_code == 400


async def test_missing_partner_id_for_match_existing_is_rejected(api_client: AsyncClient) -> None:
    use_case = _FakeResolveUseCase(result=_result())
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={"mode": "match_existing", "expected_version": 1},
        use_case=use_case,
    )
    assert response.status_code == 400
    assert use_case.commands == []  # command construction failed before the use case


async def test_partner_id_for_create_permanent_is_rejected(api_client: AsyncClient) -> None:
    use_case = _FakeResolveUseCase(result=_result())
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json={"mode": "create_permanent_supplier", "expected_version": 1, "partner_id": 10},
        use_case=use_case,
    )
    assert response.status_code == 400
    assert use_case.commands == []


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "match_existing", "expected_version": 1, "partner_id": 4010, "company_id": 99},
        {"mode": "match_existing", "expected_version": 1, "partner_id": 4010, "approved_by": "Someone"},
        {"mode": "match_existing", "expected_version": 1, "partner_id": 4010, "supplier_name": "Acme"},
        {"mode": "match_existing", "expected_version": 1, "partner_id": 4010, "supplier_tax_number": "0430367181"},
        {"mode": "match_existing", "expected_version": 1, "partner_id": 4010, "review_id": "review:other"},
    ],
)
async def test_body_cannot_set_identity_or_path_fields(api_client: AsyncClient, body: dict[str, Any]) -> None:
    response = await _post(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_DECIDE),
        json=body,
        use_case=_FakeResolveUseCase(result=_result()),
    )
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "request_validation_error"
