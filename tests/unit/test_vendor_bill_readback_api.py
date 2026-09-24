from __future__ import annotations

from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import dependencies
from app.api.routers.workbench import router
from app.api.security import AuthenticationMethod, Permission, RequestContext
from app.application.workbench.vendor_bill_readback import (
    VendorBillHeaderVerification,
    VendorBillLineVerification,
    VendorBillReadback,
    VendorBillReadbackNotFoundError,
)

REVIEW_ID = "review:b9aacadc-c67e-50b2-9183-bb730cb4709b"


class _UseCase:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = []

    def execute(self, *, review_id: str, company_id: int):
        self.calls.append((review_id, company_id))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _result() -> VendorBillReadback:
    return VendorBillReadback(
        review_id=REVIEW_ID,
        execution_id="accepted-decision-execution:4d94e148-2b63-5bf9-b28b-1abdfcd94b61",
        artifact_id="63",
        header=VendorBillHeaderVerification(
            move_id=63,
            company_id=7,
            state="draft",
            move_type="in_invoice",
            partner_id=439,
            currency="TRY",
            amount_untaxed=Decimal("4959.80"),
            amount_tax=Decimal("991.96"),
            amount_total=Decimal("5951.76"),
        ),
        lines=(
            VendorBillLineVerification(
                line_id=101,
                move_id=63,
                account_id=247,
                product_id=None,
                quantity=Decimal("20"),
                price_unit=Decimal("74.397000"),
                tax_ids=(34,),
                price_subtotal=Decimal("1487.94"),
                price_total=Decimal("1785.53"),
            ),
        ),
    )


def _client(use_case, *, permissions=(Permission.WORKBENCH_EXECUTE,), company_id=7):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[dependencies.get_vendor_bill_readback_use_case] = lambda: use_case
    app.dependency_overrides[dependencies.get_request_context] = lambda: RequestContext(
        user_id="finance",
        company_id=company_id,
        trace_id="readback-test",
        authentication_method=AuthenticationMethod.JWT,
        permissions=permissions,
    )
    return TestClient(app)


def test_get_readback_contract_preserves_values_and_context_company() -> None:
    use_case = _UseCase(_result())
    with _client(use_case) as client:
        response = client.get(f"/api/workbench/reviews/{REVIEW_ID}/vendor-bill-readback")
    assert response.status_code == 200
    assert use_case.calls == [(REVIEW_ID, 7)]
    data = response.json()["data"]
    assert data["move_id"] == 63
    assert data["partner_id"] == 439
    assert data["currency"] == "TRY"
    assert data["amount_total"] == "5951.76"
    assert data["lines"][0]["product_id"] is None
    assert data["lines"][0]["account_id"] == 247
    assert data["lines"][0]["tax_ids"] == [34]
    assert data["lines"][0]["price_unit"] == "74.397000"


def test_arbitrary_move_id_and_other_odoo_inputs_are_rejected() -> None:
    use_case = _UseCase(_result())
    with _client(use_case) as client:
        for name in ("move_id", "model", "domain", "fields", "company_id", "partner_id"):
            response = client.get(f"/api/workbench/reviews/{REVIEW_ID}/vendor-bill-readback", params={name: "999"})
            assert response.status_code == 400
    assert use_case.calls == []


def test_permission_and_not_found_guards() -> None:
    use_case = _UseCase(_result())
    with _client(use_case, permissions=(Permission.WORKBENCH_REVIEW_READ,)) as client:
        assert client.get(f"/api/workbench/reviews/{REVIEW_ID}/vendor-bill-readback").status_code == 403
    assert use_case.calls == []

    missing = _UseCase(VendorBillReadbackNotFoundError("The persisted Vendor Bill artifact was not found in Odoo."))
    with _client(missing) as client:
        response = client.get(f"/api/workbench/reviews/{REVIEW_ID}/vendor-bill-readback")
    assert response.status_code == 404


def test_route_is_get_only_and_openapi_has_no_odoo_identity_inputs() -> None:
    matching = [
        route
        for route in router.routes
        if getattr(route, "path", None) == "/api/workbench/reviews/{review_id}/vendor-bill-readback"
    ]
    assert len(matching) == 1
    assert matching[0].methods == {"GET"}
    with _client(_UseCase(_result())) as client:
        operation = client.get("/openapi.json").json()["paths"][
            "/api/workbench/reviews/{review_id}/vendor-bill-readback"
        ]["get"]
    assert {parameter["name"] for parameter in operation.get("parameters", [])} == {"review_id"}
