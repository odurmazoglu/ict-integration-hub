"""P0-PROD-15P: read-only expense-account lookup (reader + endpoint).

Covers both layers:
  * ``OdooExpenseAccountCandidateReader`` -- the server-controlled domain/field
    list, metadata-validated field existence (never assumed, per the exact
    P0-PROD-15J/15K lesson), and fail-closed malformed-response handling.
  * ``GET /api/workbench/expense-accounts`` -- authentication/permission
    enforcement and that the endpoint never becomes a generic Odoo proxy.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.api.dependencies import get_list_expense_account_candidates_use_case, get_request_context
from app.api.security import AuthenticationMethod, InvalidTokenError, Permission, RequestContext
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workbench.expense_account_lookup import (
    ExpenseAccountCandidate,
    ListExpenseAccountCandidatesQuery,
)
from app.erp.exceptions import ErpRepositoryResponseError
from app.erp.odoo.expense_account_candidate_reader import ACCOUNT_MODEL, OdooExpenseAccountCandidateReader
from app.main import app

COMPANY_ID = 7
OTHER_COMPANY_ID = 8


# --------------------------------------------------------------------------- fake Odoo adapter


def _account_record(
    *,
    id: int,
    code: str = "770.01",
    name: str = "General Expenses",
    account_type: str = "expense",
    company_ids: list[int] | None = None,
    deprecated: bool = False,
) -> dict[str, Any]:
    return {
        "id": id,
        "code": code,
        "name": name,
        "account_type": account_type,
        "company_ids": company_ids if company_ids is not None else [COMPANY_ID],
        "deprecated": deprecated,
    }


class FakeAccountAdapter:
    """Simulates real Odoo domain evaluation for exactly the clause shapes the
    reader emits -- company_ids/account_type/deprecated/id/ilike code/name --
    so tests exercise genuine server-side scoping, not a dumb stub.
    """

    def __init__(
        self,
        *,
        records: tuple[dict[str, Any], ...] = (),
        field_records: dict[str, tuple[dict[str, Any], ...]] | None = None,
    ) -> None:
        self.records = records
        # Per-field metadata response; a field absent from this dict means "exists"
        # (default char/selection shape), an explicit empty tuple means "missing".
        self.field_records = field_records or {}
        self.search_calls: list[list[Any]] = []
        self.metadata_calls: list[str] = []

    def read_model_field_metadata(self, *, model: str, field_name: str) -> tuple[dict[str, Any], ...]:
        assert model == ACCOUNT_MODEL
        self.metadata_calls.append(field_name)
        if field_name in self.field_records:
            return self.field_records[field_name]
        return ({"name": field_name, "ttype": "char", "relation": False},)

    def search_read_all(
        self, *, model: str, domain: list[Any], fields: list[str], max_records: int | None = None
    ) -> tuple[dict[str, Any], ...]:
        assert model == ACCOUNT_MODEL
        self.search_calls.append(domain)
        matched = tuple(record for record in self.records if _matches(record, domain))
        return matched[:max_records] if max_records is not None else matched


class MalformedOdooDomainError(AssertionError):
    """Raised where real Odoo would reject the domain (P0-PROD-18C: HTTP 500)."""


_DOMAIN_OPERATORS = ("&", "|", "!")
_LEAF_OPERATORS = frozenset({"=", "in", "ilike"})


def _matches(record: dict[str, Any], domain: list[Any]) -> bool:
    """Evaluate a real Odoo prefix-notation domain against one record.

    Faithful to Odoo's shape rules rather than to what the reader happens to emit:
    top-level elements are implicitly AND-ed, ``&``/``|`` are binary and ``!`` unary
    prefix operators, and every other element must be a ``[field, operator, value]``
    leaf with a string field and a known string operator. Anything else -- e.g. the
    pre-18C nested ``["|", leaf, leaf]`` element -- raises instead of being guessed at.
    """

    if not isinstance(domain, list):
        raise MalformedOdooDomainError(f"Domain must be a list, got {domain!r}.")
    results: list[bool] = []
    position = 0
    while position < len(domain):
        value, position = _evaluate(record, domain, position)
        results.append(value)
    return all(results)


def _evaluate(record: dict[str, Any], domain: list[Any], position: int) -> tuple[bool, int]:
    if position >= len(domain):
        raise MalformedOdooDomainError("Domain operator is missing an operand.")
    element = domain[position]
    if element == "!":
        value, position = _evaluate(record, domain, position + 1)
        return not value, position
    if element in ("&", "|"):
        left, position = _evaluate(record, domain, position + 1)
        right, position = _evaluate(record, domain, position)
        return (left and right) if element == "&" else (left or right), position
    return _leaf(record, element), position + 1


def _leaf(record: dict[str, Any], element: Any) -> bool:
    if not isinstance(element, (list, tuple)) or len(element) != 3:
        raise MalformedOdooDomainError(f"Invalid domain element {element!r}.")
    field, op, value = element
    if not isinstance(field, str) or field in _DOMAIN_OPERATORS:
        raise MalformedOdooDomainError(f"Invalid domain leaf field {field!r}.")
    if not isinstance(op, str) or op not in _LEAF_OPERATORS:
        raise MalformedOdooDomainError(f"Invalid domain leaf operator {op!r}.")
    actual = record.get(field)
    if op == "=":
        return actual == value
    if op == "in":
        if isinstance(actual, list):
            return bool(set(actual) & set(value))
        return actual in value
    return str(value).lower() in str(actual or "").lower()


# --------------------------------------------------------------------------- reader tests


def test_find_candidates_domain_is_company_and_type_scoped_and_excludes_deprecated() -> None:
    adapter = FakeAccountAdapter(records=(_account_record(id=1),))
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    reader.find_candidates(company_id=COMPANY_ID, query=None)

    domain = adapter.search_calls[-1]
    assert ["company_ids", "in", [COMPANY_ID]] in domain
    assert ["account_type", "in", ["expense"]] in domain
    assert ["deprecated", "=", False] in domain


def test_find_candidates_returns_only_matching_fields() -> None:
    adapter = FakeAccountAdapter(records=(_account_record(id=1, code="770.01", name="General Expenses"),))
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    candidates = reader.find_candidates(company_id=COMPANY_ID, query=None)

    assert candidates == (
        ExpenseAccountCandidate(id=1, code="770.01", name="General Expenses", account_type="expense"),
    )


def test_find_candidates_excludes_non_expense_and_deprecated_accounts() -> None:
    adapter = FakeAccountAdapter(
        records=(
            _account_record(id=1, account_type="expense"),
            _account_record(id=2, account_type="expense_direct_cost"),
            _account_record(id=3, account_type="asset_current"),
            _account_record(id=4, account_type="expense", deprecated=True),
        )
    )
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    candidates = reader.find_candidates(company_id=COMPANY_ID, query=None)

    assert {c.id for c in candidates} == {1}


def test_find_candidates_is_company_isolated() -> None:
    adapter = FakeAccountAdapter(
        records=(
            _account_record(id=1, company_ids=[COMPANY_ID]),
            _account_record(id=2, company_ids=[OTHER_COMPANY_ID]),
        )
    )
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    candidates = reader.find_candidates(company_id=COMPANY_ID, query=None)

    assert {c.id for c in candidates} == {1}


def test_find_candidates_applies_text_query_as_ilike_on_code_or_name() -> None:
    adapter = FakeAccountAdapter(
        records=(
            _account_record(id=1, code="770.01", name="Office Supplies"),
            _account_record(id=2, code="771.02", name="Travel"),
        )
    )
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    candidates = reader.find_candidates(company_id=COMPANY_ID, query="Office")

    assert {c.id for c in candidates} == {1}


# --------------------------------------------------------------------------- P0-PROD-18C query domain regression


def test_no_query_domain_is_exactly_the_base_scope() -> None:
    adapter = FakeAccountAdapter(records=(_account_record(id=1),))
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    reader.find_candidates(company_id=COMPANY_ID, query=None)

    assert adapter.search_calls[-1] == [
        ["company_ids", "in", [COMPANY_ID]],
        ["account_type", "in", ["expense"]],
        ["deprecated", "=", False],
    ]


def test_query_domain_is_flat_odoo_prefix_notation() -> None:
    adapter = FakeAccountAdapter(records=(_account_record(id=1),))
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    reader.find_candidates(company_id=COMPANY_ID, query="770")

    domain = adapter.search_calls[-1]
    assert domain == [
        ["company_ids", "in", [COMPANY_ID]],
        ["account_type", "in", ["expense"]],
        ["deprecated", "=", False],
        "|",
        ["code", "ilike", "770"],
        ["name", "ilike", "770"],
    ]
    # Every non-operator element is a [str, str, value] leaf -- never a list whose
    # first item is a domain operator (the pre-18C nested shape Odoo rejected).
    for element in domain:
        if isinstance(element, str):
            assert element in ("&", "|", "!")
        else:
            assert len(element) == 3
            assert isinstance(element[0], str) and element[0] not in ("&", "|", "!")
            assert isinstance(element[1], str)


def test_query_matches_by_account_code() -> None:
    adapter = FakeAccountAdapter(
        records=(
            _account_record(id=1, code="770000", name="General Administrative Expenses"),
            _account_record(id=2, code="760000", name="Marketing Expenses"),
        )
    )
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    candidates = reader.find_candidates(company_id=COMPANY_ID, query="770")

    assert {c.id for c in candidates} == {1}


def test_query_matches_by_account_name_case_insensitively() -> None:
    adapter = FakeAccountAdapter(
        records=(
            _account_record(id=1, code="770000", name="General Administrative Expenses"),
            _account_record(id=2, code="760000", name="Marketing Expenses"),
        )
    )
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    candidates = reader.find_candidates(company_id=COMPANY_ID, query="marketing")

    assert {c.id for c in candidates} == {2}


def test_query_is_or_across_code_and_name() -> None:
    adapter = FakeAccountAdapter(
        records=(
            _account_record(id=1, code="630100", name="Research Costs"),
            _account_record(id=2, code="770000", name="General 630 Allocation"),
            _account_record(id=3, code="760000", name="Marketing Expenses"),
        )
    )
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    candidates = reader.find_candidates(company_id=COMPANY_ID, query="630")

    assert {c.id for c in candidates} == {1, 2}


def test_query_keeps_company_type_and_deprecated_scope() -> None:
    adapter = FakeAccountAdapter(
        records=(
            _account_record(id=1, code="770000", name="General Expenses"),
            _account_record(id=2, code="770001", name="General Expenses", company_ids=[OTHER_COMPANY_ID]),
            _account_record(id=3, code="770002", name="General Expenses", account_type="asset_current"),
            _account_record(id=4, code="770003", name="General Expenses", deprecated=True),
        )
    )
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    candidates = reader.find_candidates(company_id=COMPANY_ID, query="General")

    assert {c.id for c in candidates} == {1}


def test_fake_odoo_rejects_the_pre_18c_nested_or_domain() -> None:
    """Guards the fake itself: the malformed shape that escaped 15P must fail loudly."""

    nested = [
        ["company_ids", "in", [COMPANY_ID]],
        ["|", ["code", "ilike", "770"], ["name", "ilike", "770"]],
    ]
    with pytest.raises(MalformedOdooDomainError):
        _matches(_account_record(id=1), nested)
    with pytest.raises(MalformedOdooDomainError):
        _matches(_account_record(id=1), ["|", ["code", "ilike", "770"]])


def test_find_eligible_by_id_rejects_cross_company_account() -> None:
    adapter = FakeAccountAdapter(records=(_account_record(id=1, company_ids=[OTHER_COMPANY_ID]),))
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    assert reader.find_eligible_by_id(company_id=COMPANY_ID, account_id=1) is None


def test_find_eligible_by_id_rejects_non_expense_account() -> None:
    adapter = FakeAccountAdapter(records=(_account_record(id=1, account_type="asset_current"),))
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    assert reader.find_eligible_by_id(company_id=COMPANY_ID, account_id=1) is None


def test_find_eligible_by_id_accepts_valid_account() -> None:
    adapter = FakeAccountAdapter(records=(_account_record(id=1),))
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    candidate = reader.find_eligible_by_id(company_id=COMPANY_ID, account_id=1)

    assert candidate is not None
    assert candidate.id == 1


def test_missing_account_type_field_metadata_fails_closed() -> None:
    adapter = FakeAccountAdapter(records=(_account_record(id=1),), field_records={"account_type": ()})
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    with pytest.raises(ErpRepositoryResponseError):
        reader.find_candidates(company_id=COMPANY_ID, query=None)


def test_missing_code_field_metadata_fails_closed() -> None:
    adapter = FakeAccountAdapter(records=(_account_record(id=1),), field_records={"code": ()})
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    with pytest.raises(ErpRepositoryResponseError):
        reader.find_candidates(company_id=COMPANY_ID, query=None)


def test_field_metadata_is_checked_before_the_first_search() -> None:
    adapter = FakeAccountAdapter(records=(_account_record(id=1),))
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    reader.find_candidates(company_id=COMPANY_ID, query=None)

    assert set(adapter.metadata_calls) >= {"code", "name", "account_type"}


def test_missing_deprecated_field_is_tolerated_not_fatal() -> None:
    """The optional 'deprecated' field is soft-validated -- absence just means the
    domain omits that narrowing clause, never a fail-closed error."""

    adapter = FakeAccountAdapter(records=(_account_record(id=1),), field_records={"deprecated": ()})
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    candidates = reader.find_candidates(company_id=COMPANY_ID, query=None)

    assert len(candidates) == 1
    domain = adapter.search_calls[-1]
    assert not any(isinstance(c, list) and c and c[0] == "deprecated" for c in domain)


def test_malformed_record_missing_code_fails_closed() -> None:
    adapter = FakeAccountAdapter(
        records=(
            {
                "id": 1,
                "name": "x",
                "account_type": "expense",
                "company_ids": [COMPANY_ID],
                "deprecated": False,
            },
        )
    )
    reader = OdooExpenseAccountCandidateReader(adapter=adapter)

    with pytest.raises(ErpRepositoryResponseError):
        reader.find_candidates(company_id=COMPANY_ID, query=None)


def test_invalid_company_id_fails_closed() -> None:
    reader = OdooExpenseAccountCandidateReader(adapter=FakeAccountAdapter())
    with pytest.raises(ErpRepositoryResponseError):
        reader.find_candidates(company_id=0, query=None)
    with pytest.raises(ErpRepositoryResponseError):
        reader.find_eligible_by_id(company_id=COMPANY_ID, account_id=0)


# --------------------------------------------------------------------------- query DTO validation


def test_query_rejects_control_characters() -> None:
    with pytest.raises(WorkbenchContractError):
        ListExpenseAccountCandidatesQuery(company_id=COMPANY_ID, query="bad\x00value")


def test_query_rejects_overlong_text() -> None:
    with pytest.raises(WorkbenchContractError):
        ListExpenseAccountCandidatesQuery(company_id=COMPANY_ID, query="x" * 65)


def test_query_strips_whitespace() -> None:
    query = ListExpenseAccountCandidatesQuery(company_id=COMPANY_ID, query="  770  ")
    assert query.query == "770"


# --------------------------------------------------------------------------- endpoint tests


def _context(*permissions: Permission, company_id: int = COMPANY_ID) -> RequestContext:
    return RequestContext(
        user_id="op-1",
        user_name="Ops One",
        company_id=company_id,
        permissions=permissions,
        trace_id="trace-expense-accounts",
        authentication_method=AuthenticationMethod.JWT,
    )


class _FakeListUseCase:
    def __init__(self, *, candidates: tuple[ExpenseAccountCandidate, ...] = ()) -> None:
        self.candidates = candidates
        self.queries: list[ListExpenseAccountCandidatesQuery] = []

    def execute(self, query: ListExpenseAccountCandidatesQuery) -> tuple[ExpenseAccountCandidate, ...]:
        self.queries.append(query)
        return self.candidates


async def _get(
    api_client: AsyncClient,
    *,
    context: RequestContext,
    use_case=None,
    params: dict[str, str] | None = None,
):
    app.dependency_overrides[get_request_context] = lambda: context
    if use_case is not None:
        app.dependency_overrides[get_list_expense_account_candidates_use_case] = lambda: use_case
    try:
        return await api_client.get("/api/workbench/expense-accounts", params=params or {})
    finally:
        app.dependency_overrides.clear()


async def test_endpoint_happy_path_scopes_company_from_context_not_query(api_client: AsyncClient) -> None:
    use_case = _FakeListUseCase(
        candidates=(ExpenseAccountCandidate(id=1, code="770.01", name="General Expenses", account_type="expense"),)
    )
    response = await _get(
        api_client,
        context=_context(Permission.WORKBENCH_EXPENSE_ACCOUNT_READ, company_id=COMPANY_ID),
        use_case=use_case,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data == [{"id": 1, "code": "770.01", "name": "General Expenses", "account_type": "expense"}]
    assert use_case.queries[0].company_id == COMPANY_ID


async def test_endpoint_rejects_caller_supplied_company_id(api_client: AsyncClient) -> None:
    use_case = _FakeListUseCase()
    response = await _get(
        api_client,
        context=_context(Permission.WORKBENCH_EXPENSE_ACCOUNT_READ, company_id=COMPANY_ID),
        use_case=use_case,
        params={"company_id": "999"},
    )
    assert response.status_code == 200
    # company_id is not an accepted query param at all -- the context's company_id
    # is what the use case actually receives, never a caller-supplied override.
    assert use_case.queries[0].company_id == COMPANY_ID


async def test_endpoint_missing_permission_maps_to_403(api_client: AsyncClient) -> None:
    response = await _get(
        api_client,
        context=_context(Permission.WORKBENCH_REVIEW_READ),
        use_case=_FakeListUseCase(),
    )
    assert response.status_code == 403


async def test_endpoint_authentication_failure_maps_to_401(api_client: AsyncClient) -> None:
    app.dependency_overrides[get_request_context] = lambda: (_ for _ in ()).throw(
        InvalidTokenError("Bearer token is invalid.")
    )
    try:
        response = await api_client.get(
            "/api/workbench/expense-accounts",
            headers={"Authorization": "Bearer secret-token", "X-Trace-ID": "trace-401"},
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 401
    assert response.json()["trace_id"] == "trace-401"
    assert "secret-token" not in response.text


async def test_endpoint_query_too_long_is_rejected(api_client: AsyncClient) -> None:
    response = await _get(
        api_client,
        context=_context(Permission.WORKBENCH_EXPENSE_ACCOUNT_READ),
        use_case=_FakeListUseCase(),
        params={"query": "x" * 65},
    )
    assert response.status_code == 400
