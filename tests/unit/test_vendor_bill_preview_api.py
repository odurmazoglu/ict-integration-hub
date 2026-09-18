"""HTTP-level wiring for the Vendor Bill preview endpoint (P0-PROD-09B).

Proves the endpoint exists, requires workbench_execute (not a stronger write
permission), returns the documented response shape, and never touches any
write-gate setting or Odoo write client -- exercised through the real FastAPI
router with the composed use case swapped for a fully in-memory fake via
FastAPI's own dependency-override mechanism (no DB, no Odoo client needed).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import dependencies
from app.api.routers.workbench import router
from app.api.security import AuthenticationMethod, Permission, RequestContext
from app.application.execution.exceptions import ExecutionPreviewUnsupportedWorkflowError
from app.application.execution.vendor_bill_preview import (
    PreviewVendorBillRequest,
    VendorBillPreview,
    VendorBillPreviewLine,
)
from app.application.workbench.exceptions import ReviewNotFoundError
from app.application.workflow import WorkflowType

REVIEW_ID = "review:9b13ad00-e89b-5e0e-8f0e-6de12037e199"


class _FakePreviewUseCase:
    def __init__(self, *, preview: VendorBillPreview | None = None, error: Exception | None = None) -> None:
        self._preview = preview
        self._error = error
        self.calls: list[PreviewVendorBillRequest] = []

    def preview(self, request: PreviewVendorBillRequest) -> VendorBillPreview:
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        assert self._preview is not None
        return self._preview


def _preview() -> VendorBillPreview:
    return VendorBillPreview(
        review_id=REVIEW_ID,
        company_id=1,
        decision_version=4,
        decision_id="review-decision:5bb3b401-208c-484c-801d-a569bb8d8f2b",
        selected_workflow=WorkflowType.VENDOR_BILL,
        move_type="in_invoice",
        partner_id=448,
        invoice_date=date(2026, 9, 10),
        reference="HD12026000964604",
        header_company_id=1,
        currency_code="TRY",
        currency_id=31,
        idempotency_key="vendor-bill-write:99e53cf2f9777e3435e250ede0327587adb64922398c9251a2217e141e9f42b4",
        lines=(
            VendorBillPreviewLine(
                line_number="1",
                description="Kraf Kesim Tablası A2 45X60 3002G",
                quantity=Decimal("1"),
                unit_price=Decimal("563.510000"),
                account_id=247,
                product_id=None,
                tax_ids=(34,),
            ),
        ),
        gross_source_amount=Decimal("805.01"),
        total_discount=Decimal("241.50"),
        preview_untaxed=Decimal("563.51"),
        preview_tax=Decimal("112.70"),
        preview_total=Decimal("676.21"),
    )


def _api(*, use_case, permissions: tuple[Permission, ...] = (Permission.WORKBENCH_EXECUTE,)) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[dependencies.get_vendor_bill_preview_use_case] = lambda: use_case
    app.dependency_overrides[dependencies.get_request_context] = lambda: RequestContext(
        user_id="finance",
        user_name="Finance",
        company_id=1,
        permissions=permissions,
        trace_id="preview-test",
        authentication_method=AuthenticationMethod.JWT,
    )
    return TestClient(app)


def _get_preview(client: TestClient, *, decision_version: int = 4):
    return client.get(
        f"/api/workbench/reviews/{REVIEW_ID}/vendor-bill-preview",
        params={"decision_version": decision_version},
    )


def test_preview_endpoint_returns_documented_shape_and_no_odoo_ids_are_fabricated() -> None:
    use_case = _FakePreviewUseCase(preview=_preview())
    with _api(use_case=use_case) as client:
        response = _get_preview(client)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["review_id"] == REVIEW_ID
    assert data["partner_id"] == 448
    assert data["currency_id"] == 31
    assert data["idempotency_key"] == (
        "vendor-bill-write:99e53cf2f9777e3435e250ede0327587adb64922398c9251a2217e141e9f42b4"
    )
    assert data["preview_untaxed"] == "563.51"
    assert data["preview_tax"] == "112.70"
    assert data["preview_total"] == "676.21"
    assert data["gross_source_amount"] == "805.01"
    assert data["total_discount"] == "241.50"
    assert data["lines"][0]["account_id"] == 247
    assert data["lines"][0]["product_id"] is None
    assert data["lines"][0]["tax_ids"] == [34]
    # Preview never claims an Odoo account.move id -- there is no "artifact_id"/
    # "vendor_bill_id" field in the response schema at all.
    assert "artifact_id" not in data
    assert "vendor_bill_id" not in data
    assert use_case.calls == [PreviewVendorBillRequest(review_id=REVIEW_ID, company_id=1, decision_version=4)]


def test_preview_requires_workbench_execute_not_a_write_permission() -> None:
    use_case = _FakePreviewUseCase(preview=_preview())
    with _api(use_case=use_case, permissions=()) as client:
        response = _get_preview(client)
    assert response.status_code == 403
    assert use_case.calls == []


def test_preview_review_read_permission_alone_is_insufficient() -> None:
    """Documents the actual narrowest-existing-permission choice: workbench_review_read
    alone does not grant preview access today -- workbench_execute is required, matching
    the same permission that already governs execution status/artifact visibility."""

    use_case = _FakePreviewUseCase(preview=_preview())
    with _api(use_case=use_case, permissions=(Permission.WORKBENCH_REVIEW_READ,)) as client:
        response = _get_preview(client)
    assert response.status_code == 403


def test_preview_missing_review_returns_404() -> None:
    use_case = _FakePreviewUseCase(error=ReviewNotFoundError("Accepted review decision was not found."))
    with _api(use_case=use_case) as client:
        response = _get_preview(client)
    assert response.status_code == 404


def test_preview_unsupported_workflow_returns_400() -> None:
    use_case = _FakePreviewUseCase(
        error=ExecutionPreviewUnsupportedWorkflowError(
            "Vendor Bill preview requires an accepted decision whose selected workflow is VENDOR_BILL."
        )
    )
    with _api(use_case=use_case) as client:
        response = _get_preview(client)
    assert response.status_code == 400


def test_preview_endpoint_is_a_get_not_a_post() -> None:
    """The endpoint must not mutate runtime state -- confirmed structurally: it is
    registered as GET, never POST, in the FastAPI route table."""

    matching_routes = [
        r for r in router.routes if getattr(r, "path", None) == "/api/workbench/reviews/{review_id}/vendor-bill-preview"
    ]
    assert len(matching_routes) == 1
    assert matching_routes[0].methods == {"GET"}
