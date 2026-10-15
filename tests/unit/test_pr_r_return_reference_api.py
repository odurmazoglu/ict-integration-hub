"""PR R: operator-triggered return-reference lookup endpoint."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api import dependencies
from app.api.routers.workbench import router
from app.api.security import AuthenticationMethod, Permission, RequestContext
from app.application.return_reference import (
    CandidateEvaluation,
    CheckResult,
    OutboxInvoiceCandidate,
    ReferenceResult,
    ReturnReferenceCheck,
    ReturnReferenceLookupResult,
    ReturnReferenceLookupUnavailableError,
    ReturnReferenceNotApplicableError,
    ReturnReferenceStatus,
    SearchOutcome,
)
from app.application.workbench.exceptions import ReviewNotFoundError
from tests.unit.pr_r_fixtures import CUSTOMER_VKN, ORIGINAL_ETTN, REFERENCE_NUMBER

REVIEW_ID = "review:pr-r-synthetic-0001"
PATH = f"/api/workbench/reviews/{REVIEW_ID}/return-reference-lookup"
BOTH = (Permission.WORKBENCH_REVIEW_READ, Permission.INVOICE_DOCUMENT_READ)


class _UseCase:
    def __init__(self, result) -> None:
        self.result = result
        self.queries = []

    def execute(self, query):
        self.queries.append(query)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _result() -> ReturnReferenceLookupResult:
    candidate = OutboxInvoiceCandidate(
        invoice_number=REFERENCE_NUMBER,
        ettn=ORIGINAL_ETTN,
        invoice_type="Sales",
        scenario="ComercialInvoice",
        status="Approved",
        buyer_tax_number=CUSTOMER_VKN,
        currency="TRY",
        issue_date=date(2026, 9, 10),
        archived=False,
    )
    return ReturnReferenceLookupResult(
        review_id=REVIEW_ID,
        company_id=7,
        review_version=3,
        return_invoice_type_code="IADE",
        status=ReturnReferenceStatus.MATCHED,
        references=(
            ReferenceResult(
                position=1,
                invoice_number=REFERENCE_NUMBER,
                issue_date=date(2026, 9, 10),
                document_type_code="IADE",
                status=ReturnReferenceStatus.MATCHED,
                searches=(
                    SearchOutcome(archived=False, completed=True, provider_total=1, returned=1, exact=1),
                    SearchOutcome(archived=True, completed=True, provider_total=0, returned=0, exact=0),
                ),
                candidates=(
                    CandidateEvaluation(
                        candidate=candidate,
                        checks=tuple(CheckResult(check, True) for check in ReturnReferenceCheck),
                    ),
                ),
            ),
        ),
        provider_calls=2,
        company_identity_verified=True,
    )


def _client(use_case, *, permissions=BOTH, company_id=7) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[dependencies.get_lookup_return_reference_use_case] = lambda: use_case
    app.dependency_overrides[dependencies.get_request_context] = lambda: RequestContext(
        user_id="operator",
        company_id=company_id,
        trace_id="pr-r-test",
        authentication_method=AuthenticationMethod.JWT,
        permissions=permissions,
    )
    return TestClient(app)


def test_lookup_returns_informational_result_scoped_to_context_company() -> None:
    use_case = _UseCase(_result())

    with _client(use_case) as client:
        response = client.post(PATH)

    assert response.status_code == 200
    (query,) = use_case.queries
    assert (query.review_id, query.company_id) == (REVIEW_ID, 7)
    data = response.json()["data"]
    assert data["status"] == "MATCHED"
    assert data["informational_only"] is True
    assert data["provider_calls"] == 2
    assert data["company_identity_verified"] is True
    reference = data["references"][0]
    assert reference["invoice_number"] == REFERENCE_NUMBER
    assert reference["issue_date"] == "2026-09-10"
    assert [search["archived"] for search in reference["searches"]] == [False, True]
    candidate = reference["candidates"][0]
    assert candidate["ettn"] == ORIGINAL_ETTN
    assert candidate["invoice_type"] == "Sales"
    assert candidate["scenario"] == "ComercialInvoice"
    assert "buyer_tax_number" not in candidate
    assert {check["check"] for check in candidate["checks"]} == {check.value for check in ReturnReferenceCheck}


@pytest.mark.parametrize(
    "permissions",
    [(Permission.WORKBENCH_REVIEW_READ,), (Permission.INVOICE_DOCUMENT_READ,), ()],
)
def test_lookup_requires_review_read_and_provider_document_permission(permissions) -> None:
    use_case = _UseCase(_result())

    with _client(use_case, permissions=permissions) as client:
        response = client.post(PATH)

    assert response.status_code == 403
    assert use_case.queries == []


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (ReturnReferenceNotApplicableError("not a return"), 409),
        (ReturnReferenceLookupUnavailableError("down"), 503),
        (ReviewNotFoundError("Review item was not found."), 404),
    ],
)
def test_lookup_errors_are_mapped(error: Exception, status_code: int) -> None:
    with _client(_UseCase(error)) as client:
        response = client.post(PATH)

    assert response.status_code == status_code
    assert response.json()["success"] is False


def test_lookup_rejects_query_parameters() -> None:
    use_case = _UseCase(_result())

    with _client(use_case) as client:
        response = client.post(f"{PATH}?archived=true")

    assert response.status_code == 400
    assert use_case.queries == []


def test_lookup_is_post_only() -> None:
    with _client(_UseCase(_result())) as client:
        assert client.get(PATH).status_code == 405


def _dependency_calls(dependant) -> set[object]:
    calls = set()
    for dependency in dependant.dependencies:
        calls.add(dependency.call)
        calls |= _dependency_calls(dependency)
    return calls


@pytest.mark.parametrize(
    "path",
    ["/api/workbench/reviews", "/api/workbench/reviews/{review_id}"],
)
def test_workbench_review_reads_never_build_the_lookup_or_uyumsoft_client(path: str) -> None:
    (route,) = [r for r in router.routes if isinstance(r, APIRoute) and r.path == path and "GET" in r.methods]

    calls = _dependency_calls(route.dependant)

    assert dependencies.get_lookup_return_reference_use_case not in calls
    assert dependencies.get_uyumsoft_client not in calls
