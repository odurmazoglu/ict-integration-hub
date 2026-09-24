"""P0-PROD-18D: read-only product/category purchase-account discovery.

Covers every layer:
  * ``OdooPurchaseAccountDiscoveryReader`` against a fake Odoo that evaluates real
    domain shapes, stores raw Odoo field shapes (``[id, name]`` many2ones, ``False``
    for empty), serves ``ir.model.fields`` metadata, and exposes read methods only;
  * the pure resolution rules (override precedence, no fallback on an unusable
    override, category fallback, storable/fiscal-position fail-closed);
  * the use cases' company-context gate;
  * ``GET /api/workbench/resale-product-categories`` and
    ``GET /api/workbench/products/{product_id}/purchase-account``.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.api.dependencies import (
    get_list_category_purchase_accounts_use_case,
    get_product_purchase_account_use_case,
    get_request_context,
)
from app.api.security import AuthenticationMethod, Permission, RequestContext
from app.application.workbench.exceptions import (
    PurchaseAccountCompanyContextError,
    PurchaseAccountDiscoveryError,
    PurchaseAccountProductNotFoundError,
    WorkbenchContractError,
)
from app.application.workbench.purchase_account_discovery import (
    MAX_DISCOVERY_CATEGORIES,
    CategoryPurchaseAccountConfiguration,
    FiscalPositionMapping,
    GetProductPurchaseAccountQuery,
    ListCategoryPurchaseAccountsQuery,
    ProductPurchaseAccountResolution,
    PurchaseAccountBlocker,
    PurchaseAccountSource,
    PurchaseAccountStatus,
    PurchaseAccountView,
    ResolvedPurchaseAccount,
)
from app.application.workbench.purchase_account_discovery_use_cases import (
    GetProductPurchaseAccountUseCase,
    ListCategoryPurchaseAccountsUseCase,
)
from app.erp.exceptions import ErpRepositoryResponseError
from app.erp.odoo.purchase_account_discovery_reader import (
    ACCOUNT_MODEL,
    CATEGORY_MODEL,
    COMPANY_MODEL,
    OPTIONAL_FIELDS,
    PRODUCT_MODEL,
    REQUIRED_FIELDS,
    TEMPLATE_MODEL,
    OdooPurchaseAccountDiscoveryReader,
)
from app.main import app

COMPANY_ID = 1
OTHER_COMPANY_ID = 2

# --------------------------------------------------------------------------- fake read-only Odoo


class MalformedOdooDomainError(AssertionError):
    pass


def _m2o(record_id: int | None, name: str = "x") -> Any:
    return [record_id, name] if record_id is not None else False


def _company(id: int) -> dict[str, Any]:
    return {"id": id, "name": f"Company {id}"}


def _account(
    id: int,
    *,
    code: str = "150000",
    name: str = "Raw Materials And Supplies",
    account_type: str = "asset_current",
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
        "active": True,
    }


def _category(id: int, *, name: str = "Goods", account_id: int | None = 29, complete_name: Any = None) -> dict:
    return {
        "id": id,
        "name": name,
        "complete_name": complete_name if complete_name is not None else f"All / {name}",
        "property_account_expense_categ_id": _m2o(account_id, "account"),
    }


def _template(
    id: int,
    *,
    name: str = "Widget",
    categ_id: int | None = 5,
    override_account_id: int | None = None,
    product_type: str = "consu",
    is_storable: bool = False,
    company_id: int | None = None,
    active: bool = True,
) -> dict[str, Any]:
    return {
        "id": id,
        "name": name,
        "categ_id": _m2o(categ_id, "category"),
        "property_account_expense_id": _m2o(override_account_id, "account"),
        "type": product_type,
        "is_storable": is_storable,
        "company_id": _m2o(company_id, "company"),
        "active": active,
    }


def _variant(id: int, *, template_id: int, company_id: int | None = None, active: bool = True) -> dict[str, Any]:
    return {
        "id": id,
        "product_tmpl_id": _m2o(template_id, "template"),
        "company_id": _m2o(company_id, "company"),
        "active": active,
    }


_FIELD_METADATA: dict[str, dict[str, tuple[str, str | bool]]] = {
    model: {
        name: (ttype, relation or False) for name, ttype, relation in REQUIRED_FIELDS[model] + OPTIONAL_FIELDS[model]
    }
    for model in REQUIRED_FIELDS
}


class FakeOdoo:
    """Read-only fake of ``OdooReadOnlyAdapter``: no create/write/unlink attribute exists at all."""

    def __init__(
        self,
        *,
        companies: tuple[dict[str, Any], ...] = (_company(COMPANY_ID),),
        categories: tuple[dict[str, Any], ...] = (),
        templates: tuple[dict[str, Any], ...] = (),
        variants: tuple[dict[str, Any], ...] = (),
        accounts: tuple[dict[str, Any], ...] = (),
        metadata_overrides: dict[tuple[str, str], tuple[dict[str, Any], ...]] | None = None,
    ) -> None:
        self.records = {
            COMPANY_MODEL: companies,
            CATEGORY_MODEL: categories,
            TEMPLATE_MODEL: templates,
            PRODUCT_MODEL: variants,
            ACCOUNT_MODEL: accounts,
        }
        self.metadata_overrides = metadata_overrides or {}
        self.calls: list[tuple[str, str]] = []
        self.searches: list[tuple[str, list[Any], list[str]]] = []

    def read_model_field_metadata(self, *, model: str, field_name: str) -> tuple[dict[str, Any], ...]:
        self.calls.append(("read_model_field_metadata", model))
        if (model, field_name) in self.metadata_overrides:
            return self.metadata_overrides[(model, field_name)]
        spec = _FIELD_METADATA.get(model, {}).get(field_name)
        if spec is None:
            return ()
        return ({"name": field_name, "ttype": spec[0], "relation": spec[1]},)

    def search_read(
        self, *, model: str, domain: list[Any], fields: list[str], limit: int | None = None, offset: int = 0
    ) -> tuple[dict[str, Any], ...]:
        self.calls.append(("search_read", model))
        return self._search(model, domain, fields)[offset : (offset + limit) if limit is not None else None]

    def search_read_all(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        page_size: int | None = None,
        max_records: int | None = None,
    ) -> tuple[dict[str, Any], ...]:
        self.calls.append(("search_read_all", model))
        matched = self._search(model, domain, fields)
        return matched[:max_records] if max_records is not None else matched

    def _search(self, model: str, domain: list[Any], fields: list[str]) -> tuple[dict[str, Any], ...]:
        self.searches.append((model, domain, list(fields)))
        for field_name in fields:
            if field_name != "id" and field_name not in _FIELD_METADATA.get(model, {}):
                raise AssertionError(f"Reader requested an unexpected field {model}.{field_name}.")
        archived_visible = any(
            isinstance(leaf, list) and leaf[:2] == ["active", "in"] and False in leaf[2] for leaf in domain
        )
        matched = []
        for record in self.records[model]:
            if not archived_visible and record.get("active") is False:
                continue
            if _matches(record, domain):
                matched.append({name: record[name] for name in fields if name in record})
        return tuple(matched)


def _matches(record: dict[str, Any], domain: list[Any]) -> bool:
    if not isinstance(domain, list):
        raise MalformedOdooDomainError("Domain must be a list.")
    for leaf in domain:
        if not isinstance(leaf, list) or len(leaf) != 3 or not isinstance(leaf[0], str) or leaf[1] not in ("=", "in"):
            raise MalformedOdooDomainError(f"Unsupported domain element {leaf!r}.")
        field_name, op, value = leaf
        actual = record.get(field_name)
        if isinstance(actual, list) and len(actual) == 2 and isinstance(actual[1], str):
            actual = actual[0]  # many2one compares by id
        if op == "=":
            if actual != value:
                return False
        elif isinstance(actual, list):  # many2many
            if not set(actual) & set(value):
                return False
        elif actual not in value:
            return False
    return True


READ_METHODS = {"search_read", "search_read_all", "read_model_field_metadata"}


def _reader(fake: FakeOdoo) -> OdooPurchaseAccountDiscoveryReader:
    return OdooPurchaseAccountDiscoveryReader(adapter=fake)  # type: ignore[arg-type]


def _standard_odoo(**overrides: Any) -> FakeOdoo:
    values: dict[str, Any] = {
        "categories": (_category(5, name="Goods", account_id=29),),
        "templates": (_template(10, categ_id=5),),
        "variants": (_variant(100, template_id=10),),
        "accounts": (_account(29),),
    }
    values.update(overrides)
    return FakeOdoo(**values)


def _get_product(fake: FakeOdoo, product_id: int = 100) -> ProductPurchaseAccountResolution:
    return GetProductPurchaseAccountUseCase(reader=_reader(fake)).execute(
        GetProductPurchaseAccountQuery(company_id=COMPANY_ID, product_id=product_id)
    )


def _list_categories(fake: FakeOdoo) -> tuple[CategoryPurchaseAccountConfiguration, ...]:
    return ListCategoryPurchaseAccountsUseCase(reader=_reader(fake)).execute(
        ListCategoryPurchaseAccountsQuery(company_id=COMPANY_ID)
    )


# --------------------------------------------------------------------------- category discovery


def test_category_with_configured_valid_account() -> None:
    (category,) = _list_categories(_standard_odoo())

    assert category == CategoryPurchaseAccountConfiguration(
        category_id=5,
        category_name="Goods",
        category_complete_name="All / Goods",
        purchase_account=ResolvedPurchaseAccount(
            status=PurchaseAccountStatus.VALID,
            configured_account_id=29,
            account=PurchaseAccountView(
                account_id=29,
                code="150000",
                name="Raw Materials And Supplies",
                account_type="asset_current",
                deprecated=False,
            ),
        ),
    )


def test_category_account_type_is_reported_never_filtered() -> None:
    fake = _standard_odoo(accounts=(_account(29, code="153000", name="Trade Goods", account_type="asset_current"),))

    (category,) = _list_categories(fake)

    assert category.purchase_account.status is PurchaseAccountStatus.VALID
    assert category.purchase_account.account is not None
    assert category.purchase_account.account.account_type == "asset_current"


def test_category_without_account_is_not_configured() -> None:
    (category,) = _list_categories(_standard_odoo(categories=(_category(5, account_id=None),)))

    assert category.purchase_account == ResolvedPurchaseAccount(status=PurchaseAccountStatus.NOT_CONFIGURED)


def test_category_account_owned_by_other_company_is_unavailable() -> None:
    fake = _standard_odoo(accounts=(_account(29, company_ids=[OTHER_COMPANY_ID]),))

    (category,) = _list_categories(fake)

    assert category.purchase_account == ResolvedPurchaseAccount(
        status=PurchaseAccountStatus.UNAVAILABLE, configured_account_id=29
    )


def test_category_account_missing_or_archived_is_unavailable() -> None:
    archived = {**_account(29), "active": False}

    for accounts in ((), (archived,)):
        (category,) = _list_categories(_standard_odoo(accounts=accounts))
        assert category.purchase_account.status is PurchaseAccountStatus.UNAVAILABLE
        assert category.purchase_account.account is None


def test_category_account_deprecated_is_reported_as_deprecated() -> None:
    (category,) = _list_categories(_standard_odoo(accounts=(_account(29, deprecated=True),)))

    assert category.purchase_account.status is PurchaseAccountStatus.DEPRECATED
    assert category.purchase_account.account is not None
    assert category.purchase_account.account.deprecated is True


def test_categories_are_sorted_and_accounts_read_once_company_scoped() -> None:
    fake = _standard_odoo(
        categories=(
            _category(7, name="Services", account_id=30),
            _category(5, name="Goods", account_id=29),
            _category(6, name="Expenses", account_id=29),
        ),
        accounts=(_account(29), _account(30, code="770000", name="General Administrative Expenses")),
    )

    categories = _list_categories(fake)

    assert [c.category_id for c in categories] == [5, 6, 7]
    account_searches = [s for s in fake.searches if s[0] == ACCOUNT_MODEL]
    assert len(account_searches) == 1
    assert account_searches[0][1] == [["id", "in", [29, 30]], ["company_ids", "in", [COMPANY_ID]]]


def test_category_listing_exceeding_bound_fails_closed_instead_of_truncating() -> None:
    fake = _standard_odoo(
        categories=tuple(_category(i, name=f"C{i}") for i in range(1, MAX_DISCOVERY_CATEGORIES + 2)),
    )

    with pytest.raises(ErpRepositoryResponseError):
        _list_categories(fake)


def test_duplicate_category_ids_fail_closed() -> None:
    with pytest.raises(ErpRepositoryResponseError):
        _list_categories(_standard_odoo(categories=(_category(5), _category(5))))


def test_optional_complete_name_absent_from_metadata_is_not_requested() -> None:
    fake = _standard_odoo(metadata_overrides={(CATEGORY_MODEL, "complete_name"): ()})

    (category,) = _list_categories(fake)

    assert category.category_complete_name is None
    category_fields = next(s[2] for s in fake.searches if s[0] == CATEGORY_MODEL)
    assert category_fields == ["id", "name", "property_account_expense_categ_id"]


def test_optional_deprecated_absent_from_metadata_is_reported_as_unknown() -> None:
    fake = _standard_odoo(metadata_overrides={(ACCOUNT_MODEL, "deprecated"): ()})

    (category,) = _list_categories(fake)

    assert category.purchase_account.status is PurchaseAccountStatus.VALID
    assert category.purchase_account.account is not None
    assert category.purchase_account.account.deprecated is None


# --------------------------------------------------------------------------- metadata / malformed data fail closed


@pytest.mark.parametrize(
    ("model", "field_name", "metadata"),
    [
        (CATEGORY_MODEL, "property_account_expense_categ_id", ()),
        (
            CATEGORY_MODEL,
            "property_account_expense_categ_id",
            ({"name": "property_account_expense_categ_id", "ttype": "many2one", "relation": "account.journal"},),
        ),
        (
            CATEGORY_MODEL,
            "property_account_expense_categ_id",
            ({"name": "property_account_expense_categ_id", "ttype": "char", "relation": False},),
        ),
        (CATEGORY_MODEL, "name", ({"name": "name", "ttype": "char"}, {"name": "name", "ttype": "char"})),
        (ACCOUNT_MODEL, "company_ids", ()),
        (ACCOUNT_MODEL, "code", ({"name": "other", "ttype": "char", "relation": False},)),
    ],
)
def test_missing_or_mismatched_required_category_metadata_fails_closed(
    model: str, field_name: str, metadata: tuple[dict[str, Any], ...]
) -> None:
    fake = _standard_odoo(metadata_overrides={(model, field_name): metadata})

    with pytest.raises(ErpRepositoryResponseError):
        _list_categories(fake)


@pytest.mark.parametrize(
    ("model", "field_name"),
    [
        (TEMPLATE_MODEL, "property_account_expense_id"),
        (TEMPLATE_MODEL, "categ_id"),
        (PRODUCT_MODEL, "product_tmpl_id"),
        (PRODUCT_MODEL, "company_id"),
    ],
)
def test_missing_required_product_metadata_fails_closed(model: str, field_name: str) -> None:
    fake = _standard_odoo(metadata_overrides={(model, field_name): ()})

    with pytest.raises(ErpRepositoryResponseError):
        _get_product(fake)


@pytest.mark.parametrize("bad_many2one", [True, 0, "29", [29], [0, "x"], [-1, "x"], [29, "x", "y"], [True, "x"], None])
def test_malformed_many2one_values_fail_closed(bad_many2one: Any) -> None:
    category = {**_category(5), "property_account_expense_categ_id": bad_many2one}

    with pytest.raises(ErpRepositoryResponseError):
        _list_categories(_standard_odoo(categories=(category,)))


@pytest.mark.parametrize(
    "bad_account",
    [
        {**_account(29), "code": False},
        {**_account(29), "company_ids": [0]},
        {**_account(29), "company_ids": "1"},
        {**_account(29), "deprecated": "no"},
        {**_account(29), "id": True},
    ],
)
def test_malformed_account_records_fail_closed(bad_account: dict[str, Any]) -> None:
    class _RawAccountsOdoo(FakeOdoo):
        # Returns the malformed account record as Odoo sent it, bypassing domain matching.
        def _search(self, model: str, domain: list[Any], fields: list[str]) -> tuple[dict[str, Any], ...]:
            if model == ACCOUNT_MODEL:
                self.searches.append((model, domain, list(fields)))
                return (bad_account,)
            return super()._search(model, domain, fields)

    fake = _RawAccountsOdoo(categories=(_category(5, account_id=29),))

    with pytest.raises(ErpRepositoryResponseError):
        _list_categories(fake)


def test_account_outside_the_requested_ids_fails_closed() -> None:
    class _ExtraAccountOdoo(FakeOdoo):
        def _search(self, model: str, domain: list[Any], fields: list[str]) -> tuple[dict[str, Any], ...]:
            if model == ACCOUNT_MODEL:
                return ({k: v for k, v in _account(99).items() if k in fields},)
            return super()._search(model, domain, fields)

    with pytest.raises(ErpRepositoryResponseError):
        _list_categories(_ExtraAccountOdoo(categories=(_category(5, account_id=29),)))


# --------------------------------------------------------------------------- company context gate


@pytest.mark.parametrize(
    "companies",
    [
        (_company(COMPANY_ID), _company(OTHER_COMPANY_ID)),
        (_company(OTHER_COMPANY_ID),),
        (),
    ],
)
def test_company_context_not_provably_the_requesting_company_fails_closed(companies: tuple) -> None:
    fake = _standard_odoo(companies=companies)

    with pytest.raises(PurchaseAccountCompanyContextError):
        _list_categories(fake)
    with pytest.raises(PurchaseAccountCompanyContextError):
        _get_product(fake)
    # Fails before reading any accounting configuration at all.
    assert {model for _method, model in fake.calls} == {COMPANY_MODEL}


# --------------------------------------------------------------------------- product purchase account


def test_product_category_fallback_when_no_override() -> None:
    resolution = _get_product(_standard_odoo())

    assert resolution.pre_fiscal_position_account_determinable is True
    assert resolution.pre_fiscal_position_account_source is PurchaseAccountSource.CATEGORY
    assert resolution.pre_fiscal_position_account is not None
    assert resolution.pre_fiscal_position_account.account_id == 29
    assert resolution.product_override == ResolvedPurchaseAccount(status=PurchaseAccountStatus.NOT_CONFIGURED)
    assert resolution.blockers == ()
    assert resolution.fiscal_position_mapping is FiscalPositionMapping.NOT_EVALUATED


def test_product_override_wins_over_category() -> None:
    fake = _standard_odoo(
        templates=(_template(10, categ_id=5, override_account_id=31),),
        accounts=(_account(29), _account(31, code="153000", name="Trade Goods")),
    )

    resolution = _get_product(fake)

    assert resolution.pre_fiscal_position_account_source is PurchaseAccountSource.PRODUCT_OVERRIDE
    assert resolution.pre_fiscal_position_account is not None
    assert resolution.pre_fiscal_position_account.account_id == 31
    assert resolution.category is not None
    assert resolution.category.purchase_account.configured_account_id == 29


@pytest.mark.parametrize(
    ("override_account", "blocker"),
    [
        (None, PurchaseAccountBlocker.PRODUCT_OVERRIDE_ACCOUNT_UNAVAILABLE),
        (_account(31, deprecated=True), PurchaseAccountBlocker.PRODUCT_OVERRIDE_ACCOUNT_DEPRECATED),
        (_account(31, company_ids=[OTHER_COMPANY_ID]), PurchaseAccountBlocker.PRODUCT_OVERRIDE_ACCOUNT_UNAVAILABLE),
    ],
)
def test_unusable_override_never_falls_back_to_category(override_account: Any, blocker: PurchaseAccountBlocker) -> None:
    accounts = (_account(29),) + ((override_account,) if override_account is not None else ())
    fake = _standard_odoo(templates=(_template(10, categ_id=5, override_account_id=31),), accounts=accounts)

    resolution = _get_product(fake)

    assert resolution.pre_fiscal_position_account_determinable is False
    assert resolution.pre_fiscal_position_account is None
    assert resolution.pre_fiscal_position_account_source is None
    assert resolution.blockers == (blocker,)
    # The (valid) category account is still visible as a fact, just never used as a fallback.
    assert resolution.category is not None
    assert resolution.category.purchase_account.status is PurchaseAccountStatus.VALID


@pytest.mark.parametrize(
    ("category", "accounts", "blocker"),
    [
        (_category(5, account_id=None), (), PurchaseAccountBlocker.CATEGORY_ACCOUNT_NOT_CONFIGURED),
        (_category(5, account_id=29), (), PurchaseAccountBlocker.CATEGORY_ACCOUNT_UNAVAILABLE),
        (
            _category(5, account_id=29),
            (_account(29, deprecated=True),),
            PurchaseAccountBlocker.CATEGORY_ACCOUNT_DEPRECATED,
        ),
    ],
)
def test_unusable_category_account_blocks(category: dict, accounts: tuple, blocker: PurchaseAccountBlocker) -> None:
    resolution = _get_product(_standard_odoo(categories=(category,), accounts=accounts))

    assert resolution.pre_fiscal_position_account_determinable is False
    assert resolution.pre_fiscal_position_account is None
    assert resolution.blockers == (blocker,)


def test_product_without_category_blocks() -> None:
    resolution = _get_product(_standard_odoo(templates=(_template(10, categ_id=None),)))

    assert resolution.category is None
    assert resolution.blockers == (PurchaseAccountBlocker.PRODUCT_CATEGORY_MISSING,)


def test_storable_product_fails_closed_on_stock_valuation() -> None:
    resolution = _get_product(_standard_odoo(templates=(_template(10, is_storable=True),)))

    assert resolution.pre_fiscal_position_account_determinable is False
    assert resolution.pre_fiscal_position_account is None
    assert resolution.blockers == (PurchaseAccountBlocker.STOCK_VALUATION_NOT_EVALUATED,)


def test_unknown_storability_blocks_goods_but_not_services() -> None:
    no_storable_field = {(TEMPLATE_MODEL, "is_storable"): ()}

    goods = _get_product(_standard_odoo(metadata_overrides=no_storable_field))
    service = _get_product(
        _standard_odoo(templates=(_template(10, product_type="service"),), metadata_overrides=no_storable_field)
    )

    assert goods.is_storable is None
    assert goods.blockers == (PurchaseAccountBlocker.STOCK_VALUATION_NOT_EVALUATED,)
    assert service.is_storable is None
    assert service.pre_fiscal_position_account_determinable is True


def test_archived_product_is_reported_inactive_not_missing() -> None:
    fake = _standard_odoo(
        templates=(_template(10, active=False),),
        variants=(_variant(100, template_id=10, active=False),),
    )

    resolution = _get_product(fake)

    assert resolution.product_active is False
    assert resolution.blockers == (PurchaseAccountBlocker.PRODUCT_INACTIVE,)


def test_shared_and_own_company_products_are_visible() -> None:
    shared = _get_product(_standard_odoo())
    own = _get_product(
        _standard_odoo(
            templates=(_template(10, company_id=COMPANY_ID),),
            variants=(_variant(100, template_id=10, company_id=COMPANY_ID),),
        )
    )

    assert shared.product_company_id is None
    assert own.product_company_id == COMPANY_ID


def test_other_company_product_is_not_found() -> None:
    fake = _standard_odoo(
        templates=(_template(10, company_id=OTHER_COMPANY_ID),),
        variants=(_variant(100, template_id=10, company_id=OTHER_COMPANY_ID),),
    )

    with pytest.raises(PurchaseAccountProductNotFoundError):
        _get_product(fake)


def test_unknown_product_is_not_found() -> None:
    with pytest.raises(PurchaseAccountProductNotFoundError):
        _get_product(_standard_odoo(), product_id=999)


def test_variant_template_company_mismatch_fails_closed() -> None:
    fake = _standard_odoo(
        templates=(_template(10, company_id=COMPANY_ID),),
        variants=(_variant(100, template_id=10, company_id=None),),
    )

    with pytest.raises(ErpRepositoryResponseError):
        _get_product(fake)


def test_product_reads_use_exact_company_scoped_domains() -> None:
    fake = _standard_odoo()

    _get_product(fake)

    domains = {model: domain for model, domain, _fields in fake.searches}
    company_scope = ["company_id", "in", [COMPANY_ID, False]]
    with_archived = ["active", "in", [True, False]]
    assert domains[PRODUCT_MODEL] == [["id", "=", 100], company_scope, with_archived]
    assert domains[TEMPLATE_MODEL] == [["id", "=", 10], company_scope, with_archived]
    assert domains[CATEGORY_MODEL] == [["id", "=", 5]]
    assert domains[ACCOUNT_MODEL] == [["id", "in", [29]], ["company_ids", "in", [COMPANY_ID]]]


def test_discovery_only_ever_calls_read_methods() -> None:
    fake = _standard_odoo(templates=(_template(10, override_account_id=31),), accounts=(_account(29), _account(31)))

    _list_categories(fake)
    _get_product(fake)

    assert {method for method, _model in fake.calls} <= READ_METHODS
    assert {model for _method, model in fake.calls} <= {
        COMPANY_MODEL,
        CATEGORY_MODEL,
        PRODUCT_MODEL,
        TEMPLATE_MODEL,
        ACCOUNT_MODEL,
    }
    for forbidden in ("create", "write", "unlink", "call_model_method"):
        assert not hasattr(fake, forbidden)


def test_inconsistent_category_read_is_an_integrity_error() -> None:
    class _WrongCategoryReader:
        def __init__(self, inner: OdooPurchaseAccountDiscoveryReader) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def find_category(self, *, category_id: int):
            return self._inner.find_category(category_id=6)

    fake = _standard_odoo(categories=(_category(5), _category(6)))
    use_case = GetProductPurchaseAccountUseCase(reader=_WrongCategoryReader(_reader(fake)))  # type: ignore[arg-type]

    with pytest.raises(PurchaseAccountDiscoveryError):
        use_case.execute(GetProductPurchaseAccountQuery(company_id=COMPANY_ID, product_id=100))


# --------------------------------------------------------------------------- DTO contracts


def test_resolution_cannot_present_an_account_while_blocked() -> None:
    view = PurchaseAccountView(account_id=29, code="150000", name="x", account_type="asset_current", deprecated=False)
    base: dict[str, Any] = {
        "product_id": 100,
        "product_template_id": 10,
        "product_name": "Widget",
        "product_active": True,
        "product_company_id": None,
        "product_type": "consu",
        "is_storable": True,
        "category": None,
        "product_override": ResolvedPurchaseAccount(status=PurchaseAccountStatus.NOT_CONFIGURED),
    }

    with pytest.raises(WorkbenchContractError):
        ProductPurchaseAccountResolution(
            **base,
            pre_fiscal_position_account=view,
            pre_fiscal_position_account_source=PurchaseAccountSource.CATEGORY,
            pre_fiscal_position_account_determinable=False,
            blockers=(PurchaseAccountBlocker.STOCK_VALUATION_NOT_EVALUATED,),
        )
    with pytest.raises(WorkbenchContractError):
        ProductPurchaseAccountResolution(
            **base,
            pre_fiscal_position_account=None,
            pre_fiscal_position_account_source=None,
            pre_fiscal_position_account_determinable=True,
        )


@pytest.mark.parametrize("product_id", [0, -1, True, "1"])
def test_product_query_rejects_invalid_ids(product_id: Any) -> None:
    with pytest.raises(WorkbenchContractError):
        GetProductPurchaseAccountQuery(company_id=COMPANY_ID, product_id=product_id)


# --------------------------------------------------------------------------- endpoints


def _context(*permissions: Permission, company_id: int = COMPANY_ID) -> RequestContext:
    return RequestContext(
        user_id="op-1",
        user_name="Ops One",
        company_id=company_id,
        permissions=permissions,
        trace_id="trace-18d",
        authentication_method=AuthenticationMethod.JWT,
    )


class _Recorder:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.queries: list[Any] = []

    def execute(self, query: Any) -> Any:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.result


async def _call(
    api_client: AsyncClient,
    path: str,
    *,
    context: RequestContext,
    categories: _Recorder | None = None,
    product: _Recorder | None = None,
    params: dict[str, str] | None = None,
    method: str = "GET",
):
    app.dependency_overrides[get_request_context] = lambda: context
    if categories is not None:
        app.dependency_overrides[get_list_category_purchase_accounts_use_case] = lambda: categories
    if product is not None:
        app.dependency_overrides[get_product_purchase_account_use_case] = lambda: product
    try:
        return await api_client.request(method, path, params=params or {})
    finally:
        app.dependency_overrides.clear()


READ = Permission.WORKBENCH_EXPENSE_ACCOUNT_READ


async def test_categories_endpoint_exact_serialization_and_company_from_context(api_client: AsyncClient) -> None:
    recorder = _Recorder(
        result=_list_categories(_standard_odoo(categories=(_category(5), _category(6, account_id=None))))
    )

    response = await _call(
        api_client, "/api/workbench/resale-product-categories", context=_context(READ), categories=recorder
    )

    assert response.status_code == 200
    assert response.json()["data"] == [
        {
            "category_id": 5,
            "category_name": "Goods",
            "category_complete_name": "All / Goods",
            "purchase_account": {
                "status": "valid",
                "configured_account_id": 29,
                "account": {
                    "account_id": 29,
                    "code": "150000",
                    "name": "Raw Materials And Supplies",
                    "account_type": "asset_current",
                    "deprecated": False,
                },
            },
        },
        {
            "category_id": 6,
            "category_name": "Goods",
            "category_complete_name": "All / Goods",
            "purchase_account": {"status": "not_configured", "configured_account_id": None, "account": None},
        },
    ]
    assert recorder.queries == [ListCategoryPurchaseAccountsQuery(company_id=COMPANY_ID)]


async def test_product_endpoint_exact_serialization(api_client: AsyncClient) -> None:
    recorder = _Recorder(result=_get_product(_standard_odoo()))

    response = await _call(
        api_client, "/api/workbench/products/100/purchase-account", context=_context(READ), product=recorder
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "product_id": 100,
        "product_template_id": 10,
        "product_name": "Widget",
        "product_active": True,
        "product_company_id": None,
        "product_type": "consu",
        "is_storable": False,
        "category": {
            "category_id": 5,
            "category_name": "Goods",
            "category_complete_name": "All / Goods",
            "purchase_account": {
                "status": "valid",
                "configured_account_id": 29,
                "account": {
                    "account_id": 29,
                    "code": "150000",
                    "name": "Raw Materials And Supplies",
                    "account_type": "asset_current",
                    "deprecated": False,
                },
            },
        },
        "product_override": {"status": "not_configured", "configured_account_id": None, "account": None},
        "pre_fiscal_position_account": {
            "account_id": 29,
            "code": "150000",
            "name": "Raw Materials And Supplies",
            "account_type": "asset_current",
            "deprecated": False,
        },
        "pre_fiscal_position_account_source": "category",
        "pre_fiscal_position_account_determinable": True,
        "blockers": [],
        "fiscal_position_mapping": "not_evaluated",
    }
    assert recorder.queries == [GetProductPurchaseAccountQuery(company_id=COMPANY_ID, product_id=100)]


async def test_product_endpoint_serializes_blockers_without_an_account(api_client: AsyncClient) -> None:
    recorder = _Recorder(result=_get_product(_standard_odoo(templates=(_template(10, is_storable=True),))))

    response = await _call(
        api_client, "/api/workbench/products/100/purchase-account", context=_context(READ), product=recorder
    )

    data = response.json()["data"]
    assert data["pre_fiscal_position_account"] is None
    assert data["pre_fiscal_position_account_source"] is None
    assert data["pre_fiscal_position_account_determinable"] is False
    assert data["blockers"] == ["stock_valuation_not_evaluated"]


@pytest.mark.parametrize(
    "path", ["/api/workbench/resale-product-categories", "/api/workbench/products/100/purchase-account"]
)
async def test_endpoints_require_expense_account_read_permission(api_client: AsyncClient, path: str) -> None:
    recorder = _Recorder()

    response = await _call(
        api_client,
        path,
        context=_context(Permission.WORKBENCH_REVIEW_READ, Permission.WORKBENCH_REVIEW_DECIDE),
        categories=recorder,
        product=recorder,
    )

    assert response.status_code == 403
    assert recorder.queries == []


@pytest.mark.parametrize(
    "params",
    [
        {"company_id": "2"},
        {"domain": "[]"},
        {"fields": "name"},
        {"model": "account.account"},
        {"query": "150"},
    ],
)
@pytest.mark.parametrize(
    "path", ["/api/workbench/resale-product-categories", "/api/workbench/products/100/purchase-account"]
)
async def test_endpoints_reject_every_query_parameter(api_client: AsyncClient, path: str, params: dict) -> None:
    recorder = _Recorder()

    response = await _call(
        api_client, path, context=_context(READ), categories=recorder, product=recorder, params=params
    )

    assert response.status_code == 400
    assert recorder.queries == []


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "path", ["/api/workbench/resale-product-categories", "/api/workbench/products/100/purchase-account"]
)
async def test_endpoints_are_get_only(api_client: AsyncClient, path: str, method: str) -> None:
    recorder = _Recorder()

    response = await _call(
        api_client, path, context=_context(READ), categories=recorder, product=recorder, method=method
    )

    assert response.status_code == 405
    assert recorder.queries == []


@pytest.mark.parametrize("product_id", ["abc", "1.5"])
async def test_product_endpoint_rejects_non_integer_ids(api_client: AsyncClient, product_id: str) -> None:
    recorder = _Recorder()

    response = await _call(
        api_client, f"/api/workbench/products/{product_id}/purchase-account", context=_context(READ), product=recorder
    )

    assert response.status_code == 400
    assert recorder.queries == []


@pytest.mark.parametrize("product_id", ["0", "-5"])
async def test_product_endpoint_rejects_non_positive_ids(api_client: AsyncClient, product_id: str) -> None:
    response = await _call(
        api_client,
        f"/api/workbench/products/{product_id}/purchase-account",
        context=_context(READ),
        product=_Recorder(),
    )

    assert response.status_code == 400


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (PurchaseAccountProductNotFoundError("nf"), 404, "purchase_account_product_not_found"),
        (PurchaseAccountCompanyContextError("ctx"), 409, "purchase_account_company_context_unverified"),
        (PurchaseAccountDiscoveryError("bad"), 500, "purchase_account_discovery_error"),
    ],
)
async def test_product_endpoint_error_mapping(
    api_client: AsyncClient, error: Exception, status_code: int, code: str
) -> None:
    response = await _call(
        api_client,
        "/api/workbench/products/100/purchase-account",
        context=_context(READ),
        product=_Recorder(error=error),
    )

    assert response.status_code == status_code
    assert response.json()["errors"][0]["code"] == code


async def test_categories_endpoint_company_context_error_is_409(api_client: AsyncClient) -> None:
    response = await _call(
        api_client,
        "/api/workbench/resale-product-categories",
        context=_context(READ),
        categories=_Recorder(error=PurchaseAccountCompanyContextError("ctx")),
    )

    assert response.status_code == 409
