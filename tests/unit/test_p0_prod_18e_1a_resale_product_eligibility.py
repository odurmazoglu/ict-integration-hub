"""P0-PROD-18E-1A: RESALE category allowlist setting and pure product-eligibility policy.

Covers:
  * ``RESALE_PRODUCT_CATEGORY_IDS`` parsing through the real ``Settings`` env source;
  * ``evaluate_resale_product_eligibility`` over evidence built by P0-PROD-18D's own pure
    ``resolve_product_purchase_account`` (never hand-rolled resolution shapes), so the
    policy is proven against exactly what discovery produces;
  * that the policy is not wired into any workflow and names no account.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsError

import app.application.workbench.resale_product_eligibility as policy_module
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workbench.purchase_account_discovery import (
    CategoryPurchaseAccountRecord,
    FiscalPositionMapping,
    ProductPurchaseAccountRecord,
    ProductPurchaseAccountResolution,
    PurchaseAccountRecord,
    PurchaseAccountSource,
    PurchaseAccountView,
    resolve_product_purchase_account,
)
from app.application.workbench.resale_product_eligibility import (
    ResaleProductBlocker,
    ResaleProductEligibility,
    evaluate_resale_product_eligibility,
    normalize_resale_category_ids,
)
from app.core.config import Settings

COMPANY_ID = 1
OTHER_COMPANY_ID = 2
PARENT_CATEGORY_ID = 5
CHILD_CATEGORY_ID = 7
#: Deliberately arbitrary: nothing in the policy may depend on a specific account.
ACCOUNT_ID = 987
ACCOUNT_CODE = "SOME_FUTURE_ACCOUNT"
ACCOUNT_TYPE = "some_future_type"
OVERRIDE_ACCOUNT_ID = 654

# --------------------------------------------------------------------------- builders


def _account(
    id: int = ACCOUNT_ID,
    *,
    code: str = ACCOUNT_CODE,
    account_type: str = ACCOUNT_TYPE,
    company_ids: tuple[int, ...] = (COMPANY_ID,),
    deprecated: bool | None = False,
) -> PurchaseAccountRecord:
    return PurchaseAccountRecord(
        id=id,
        code=code,
        name=f"Account {code}",
        account_type=account_type,
        company_ids=company_ids,
        deprecated=deprecated,
    )


def _category(id: int = CHILD_CATEGORY_ID, *, account_id: int | None = ACCOUNT_ID) -> CategoryPurchaseAccountRecord:
    return CategoryPurchaseAccountRecord(
        id=id,
        name=f"Category {id}",
        expense_account_id=account_id,
        complete_name=f"Parent / Category {id}",
    )


def _product(**overrides: object) -> ProductPurchaseAccountRecord:
    values: dict[str, object] = {
        "product_id": 100,
        "product_template_id": 200,
        "name": "Resold licence",
        "active": True,
        "company_id": COMPANY_ID,
        "product_type": "service",
        "category_id": CHILD_CATEGORY_ID,
        "override_account_id": None,
        "is_storable": False,
    }
    values.update(overrides)
    return ProductPurchaseAccountRecord(**values)  # type: ignore[arg-type]


def _resolution(
    *,
    product: ProductPurchaseAccountRecord | None = None,
    category: CategoryPurchaseAccountRecord | None = None,
    no_category: bool = False,
    accounts: tuple[PurchaseAccountRecord, ...] = (_account(),),
) -> ProductPurchaseAccountResolution:
    return resolve_product_purchase_account(
        product or _product(),
        category=None if no_category else (category or _category()),
        company_id=COMPANY_ID,
        accounts_by_id={account.id: account for account in accounts},
    )


def _evaluate(
    resolution: ProductPurchaseAccountResolution | None,
    *,
    approved: object = frozenset({CHILD_CATEGORY_ID}),
    company_id: int = COMPANY_ID,
) -> ResaleProductEligibility:
    return evaluate_resale_product_eligibility(
        resolution,
        company_id=company_id,
        approved_category_ids=approved,  # type: ignore[arg-type]
    )


def _assert_blocked(result: ResaleProductEligibility, *blockers: ResaleProductBlocker) -> None:
    assert result.eligible is False
    assert result.pre_fiscal_position_account is None
    for blocker in blockers:
        assert blocker in result.blockers


# =================================================================== configuration


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    monkeypatch.setenv("APP_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.delenv("RESALE_PRODUCT_CATEGORY_IDS", raising=False)
    return monkeypatch


def test_allowlist_defaults_to_empty(isolated_env: pytest.MonkeyPatch) -> None:
    assert Settings().resale_product_category_ids == frozenset()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[]", frozenset()),
        ("[5]", frozenset({5})),
        ("[5,7]", frozenset({5, 7})),
        ("[5,5,7,7]", frozenset({5, 7})),
        ("[ 5 , 7 ]", frozenset({5, 7})),
    ],
)
def test_allowlist_parses_json_list_of_exact_ids(isolated_env: pytest.MonkeyPatch, raw: str, expected) -> None:
    isolated_env.setenv("RESALE_PRODUCT_CATEGORY_IDS", raw)
    settings = Settings()
    assert settings.resale_product_category_ids == expected
    assert isinstance(settings.resale_product_category_ids, frozenset)


def test_allowlist_is_read_from_the_selected_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / "profile.env"
    env_file.write_text("RESALE_PRODUCT_CATEGORY_IDS=[12,4]\n", encoding="utf-8")
    monkeypatch.setenv("APP_ENV_FILE", str(env_file))
    monkeypatch.delenv("RESALE_PRODUCT_CATEGORY_IDS", raising=False)
    assert Settings().resale_product_category_ids == frozenset({4, 12})


@pytest.mark.parametrize("raw", ["[0]", "[-1]", "[5,0]", '["a"]', '["5"]', "[1.5]", "[true]", "5", "{}"])
def test_allowlist_rejects_invalid_values(isolated_env: pytest.MonkeyPatch, raw: str) -> None:
    isolated_env.setenv("RESALE_PRODUCT_CATEGORY_IDS", raw)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("raw", ["5,7", "", "not-json"])
def test_allowlist_rejects_non_json_formats(isolated_env: pytest.MonkeyPatch, raw: str) -> None:
    isolated_env.setenv("RESALE_PRODUCT_CATEGORY_IDS", raw)
    with pytest.raises(SettingsError):
        Settings()


def test_allowlist_rejects_invalid_init_values(isolated_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        Settings(resale_product_category_ids=[0])
    assert Settings(resale_product_category_ids=[3, 3]).resale_product_category_ids == frozenset({3})


def test_normalize_collapses_duplicates_and_rejects_invalid_ids() -> None:
    assert normalize_resale_category_ids([5, 5, 7]) == frozenset({5, 7})
    assert normalize_resale_category_ids(()) == frozenset()
    for bad in ([0], [-3], [True], ["5"], [1.0], "57"):
        with pytest.raises(WorkbenchContractError):
            normalize_resale_category_ids(bad)  # type: ignore[arg-type]


# =================================================================== eligibility: happy path


def test_exact_allowlisted_category_with_valid_account_is_eligible() -> None:
    result = _evaluate(_resolution())

    assert result.eligible is True
    assert result.blockers == ()
    assert result.product_id == 100
    assert result.category_id == CHILD_CATEGORY_ID
    assert result.pre_fiscal_position_account == PurchaseAccountView(
        account_id=ACCOUNT_ID,
        code=ACCOUNT_CODE,
        name=f"Account {ACCOUNT_CODE}",
        account_type=ACCOUNT_TYPE,
        deprecated=False,
    )


@pytest.mark.parametrize(
    ("account_id", "code", "account_type"),
    [
        (987, "SOME_FUTURE_ACCOUNT", "some_future_type"),
        (4242, "770123", "expense"),
        (3, "X", "asset_current"),
    ],
)
def test_any_valid_account_is_accepted_without_preference(account_id: int, code: str, account_type: str) -> None:
    account = _account(account_id, code=code, account_type=account_type)
    result = _evaluate(_resolution(category=_category(account_id=account_id), accounts=(account,)))

    assert result.eligible is True
    assert result.pre_fiscal_position_account is not None
    assert result.pre_fiscal_position_account.account_id == account_id
    assert result.pre_fiscal_position_account.code == code
    assert result.pre_fiscal_position_account.account_type == account_type


def test_account_with_unknown_deprecation_flag_is_accepted_as_18d_reports_it_valid() -> None:
    result = _evaluate(_resolution(accounts=(_account(deprecated=None),)))
    assert result.eligible is True


def test_shared_product_without_company_is_eligible() -> None:
    assert _evaluate(_resolution(product=_product(company_id=None))).eligible is True


def test_implementation_names_no_account_id_or_code() -> None:
    source = Path(policy_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)}
    assert not literals & {29, "29", 150000, "150000"}
    assert "150000" not in source
    assert "Raw Materials" not in source
    assert "asset_current" not in source


# =================================================================== eligibility: allowlist


@pytest.mark.parametrize("approved", [frozenset(), set(), (), []])
def test_empty_allowlist_rejects(approved: object) -> None:
    result = _evaluate(_resolution(), approved=approved)
    _assert_blocked(result, ResaleProductBlocker.RESALE_CATEGORY_ALLOWLIST_EMPTY)
    assert ResaleProductBlocker.CATEGORY_NOT_APPROVED_FOR_RESALE not in result.blockers


def test_category_not_allowlisted_rejects() -> None:
    result = _evaluate(_resolution(), approved=frozenset({999}))
    _assert_blocked(result, ResaleProductBlocker.CATEGORY_NOT_APPROVED_FOR_RESALE)
    assert result.category_id == CHILD_CATEGORY_ID


def test_parent_allowlisted_does_not_allow_child() -> None:
    result = _evaluate(_resolution(), approved=frozenset({PARENT_CATEGORY_ID}))
    _assert_blocked(result, ResaleProductBlocker.CATEGORY_NOT_APPROVED_FOR_RESALE)


def test_allowlist_is_matched_by_exact_id_not_name() -> None:
    renamed = replace(_category(), name="Software Licenses", complete_name="Software Licenses")
    result = _evaluate(_resolution(category=renamed), approved=frozenset({PARENT_CATEGORY_ID}))
    _assert_blocked(result, ResaleProductBlocker.CATEGORY_NOT_APPROVED_FOR_RESALE)


def test_invalid_allowlist_values_raise() -> None:
    with pytest.raises(WorkbenchContractError):
        _evaluate(_resolution(), approved=[0])


# =================================================================== eligibility: product facts


def test_unknown_product_rejects() -> None:
    result = _evaluate(None)
    _assert_blocked(result, ResaleProductBlocker.PRODUCT_UNKNOWN)
    assert result.product_id is None and result.category_id is None


def test_unknown_product_with_empty_allowlist_reports_both() -> None:
    result = _evaluate(None, approved=frozenset())
    assert result.blockers == (
        ResaleProductBlocker.RESALE_CATEGORY_ALLOWLIST_EMPTY,
        ResaleProductBlocker.PRODUCT_UNKNOWN,
    )


def test_inactive_product_rejects() -> None:
    _assert_blocked(_evaluate(_resolution(product=_product(active=False))), ResaleProductBlocker.PRODUCT_INACTIVE)


def test_other_company_product_rejects() -> None:
    result = _evaluate(_resolution(product=_product(company_id=OTHER_COMPANY_ID)))
    _assert_blocked(result, ResaleProductBlocker.PRODUCT_COMPANY_MISMATCH)


def test_missing_category_rejects() -> None:
    result = _evaluate(_resolution(product=_product(category_id=None), no_category=True))
    _assert_blocked(
        result,
        ResaleProductBlocker.PRODUCT_CATEGORY_MISSING,
        ResaleProductBlocker.PRE_FISCAL_POSITION_ACCOUNT_NOT_DETERMINABLE,
    )
    assert result.category_id is None


def test_storable_product_rejects() -> None:
    result = _evaluate(_resolution(product=_product(product_type="consu", is_storable=True)))
    _assert_blocked(
        result,
        ResaleProductBlocker.PRODUCT_STORABLE,
        ResaleProductBlocker.PRE_FISCAL_POSITION_ACCOUNT_NOT_DETERMINABLE,
    )


@pytest.mark.parametrize("product_type", ["service", "consu"])
def test_unknown_storability_rejects_even_for_services(product_type: str) -> None:
    # 18D treats an unknown flag on a service as determinable; RESALE v1 is stricter.
    result = _evaluate(_resolution(product=_product(product_type=product_type, is_storable=None)))
    _assert_blocked(result, ResaleProductBlocker.PRODUCT_STORABILITY_UNKNOWN)


def test_non_storable_goods_are_not_rejected_for_type() -> None:
    assert _evaluate(_resolution(product=_product(product_type="consu", is_storable=False))).eligible is True


@pytest.mark.parametrize(
    "override_account",
    [
        _account(OVERRIDE_ACCOUNT_ID, code="OVR"),
        _account(OVERRIDE_ACCOUNT_ID, code="OVR", deprecated=True),
        None,
    ],
)
def test_product_account_override_rejects(override_account: PurchaseAccountRecord | None) -> None:
    accounts = (_account(),) if override_account is None else (_account(), override_account)
    result = _evaluate(_resolution(product=_product(override_account_id=OVERRIDE_ACCOUNT_ID), accounts=accounts))
    _assert_blocked(result, ResaleProductBlocker.PRODUCT_ACCOUNT_OVERRIDE_CONFIGURED)


# =================================================================== eligibility: category account


def test_category_account_missing_rejects() -> None:
    result = _evaluate(_resolution(category=_category(account_id=None)))
    _assert_blocked(
        result,
        ResaleProductBlocker.CATEGORY_ACCOUNT_NOT_CONFIGURED,
        ResaleProductBlocker.PRE_FISCAL_POSITION_ACCOUNT_NOT_DETERMINABLE,
    )


@pytest.mark.parametrize(
    "accounts",
    [(), (_account(company_ids=(OTHER_COMPANY_ID,)),)],
    ids=["unreadable", "other-company"],
)
def test_category_account_unavailable_rejects(accounts: tuple[PurchaseAccountRecord, ...]) -> None:
    result = _evaluate(_resolution(accounts=accounts))
    _assert_blocked(
        result,
        ResaleProductBlocker.CATEGORY_ACCOUNT_UNAVAILABLE,
        ResaleProductBlocker.PRE_FISCAL_POSITION_ACCOUNT_NOT_DETERMINABLE,
    )


def test_category_account_deprecated_rejects() -> None:
    result = _evaluate(_resolution(accounts=(_account(deprecated=True),)))
    _assert_blocked(result, ResaleProductBlocker.CATEGORY_ACCOUNT_DEPRECATED)


def test_category_account_problem_is_reported_even_when_not_allowlisted() -> None:
    result = _evaluate(_resolution(category=_category(account_id=None)), approved=frozenset({999}))
    _assert_blocked(
        result,
        ResaleProductBlocker.CATEGORY_NOT_APPROVED_FOR_RESALE,
        ResaleProductBlocker.CATEGORY_ACCOUNT_NOT_CONFIGURED,
    )


# =================================================================== eligibility: account evidence


def test_indeterminable_pre_fiscal_account_rejects() -> None:
    result = _evaluate(_resolution(product=_product(active=False)))
    _assert_blocked(result, ResaleProductBlocker.PRE_FISCAL_POSITION_ACCOUNT_NOT_DETERMINABLE)


def test_pre_fiscal_account_differing_from_category_account_is_inconsistent() -> None:
    resolution = _resolution()
    other_view = PurchaseAccountView(
        account_id=ACCOUNT_ID + 1, code="OTHER", name="Other", account_type=ACCOUNT_TYPE, deprecated=False
    )
    tampered = replace(resolution, pre_fiscal_position_account=other_view)
    _assert_blocked(_evaluate(tampered), ResaleProductBlocker.PURCHASE_ACCOUNT_EVIDENCE_INCONSISTENT)


def test_pre_fiscal_account_from_override_source_is_inconsistent() -> None:
    tampered = replace(_resolution(), pre_fiscal_position_account_source=PurchaseAccountSource.PRODUCT_OVERRIDE)
    _assert_blocked(_evaluate(tampered), ResaleProductBlocker.PURCHASE_ACCOUNT_EVIDENCE_INCONSISTENT)


def test_blockers_are_unique_and_ordered() -> None:
    result = _evaluate(_resolution(product=_product(active=False, company_id=OTHER_COMPANY_ID)))
    assert len(result.blockers) == len(set(result.blockers))
    assert result.blockers[:2] == (
        ResaleProductBlocker.PRODUCT_INACTIVE,
        ResaleProductBlocker.PRODUCT_COMPANY_MISMATCH,
    )


# =================================================================== fiscal-position boundary


def test_fiscal_position_remains_not_evaluated() -> None:
    eligible = _evaluate(_resolution())
    blocked = _evaluate(None)
    assert eligible.fiscal_position_mapping is FiscalPositionMapping.NOT_EVALUATED
    assert blocked.fiscal_position_mapping is FiscalPositionMapping.NOT_EVALUATED
    assert set(FiscalPositionMapping) == {FiscalPositionMapping.NOT_EVALUATED}
    assert "final_account" not in ResaleProductEligibility.__dataclass_fields__
    assert "account_id" not in ResaleProductEligibility.__dataclass_fields__


# =================================================================== contract invariants


def test_result_invariants_are_enforced() -> None:
    view = _evaluate(_resolution()).pre_fiscal_position_account
    with pytest.raises(WorkbenchContractError):
        ResaleProductEligibility(eligible=True, product_id=1, category_id=1, pre_fiscal_position_account=None)
    with pytest.raises(WorkbenchContractError):
        ResaleProductEligibility(
            eligible=False,
            product_id=1,
            category_id=1,
            pre_fiscal_position_account=view,
            blockers=(ResaleProductBlocker.PRODUCT_INACTIVE,),
        )
    with pytest.raises(WorkbenchContractError):
        ResaleProductEligibility(
            eligible=True,
            product_id=1,
            category_id=1,
            pre_fiscal_position_account=view,
            blockers=(ResaleProductBlocker.PRODUCT_INACTIVE,),
        )


@pytest.mark.parametrize("company_id", [0, -1, True])
def test_invalid_company_id_raises(company_id: object) -> None:
    with pytest.raises(WorkbenchContractError):
        _evaluate(_resolution(), company_id=company_id)  # type: ignore[arg-type]


def test_non_resolution_input_raises() -> None:
    with pytest.raises(WorkbenchContractError):
        _evaluate(object())  # type: ignore[arg-type]


# =================================================================== no workflow wiring


def test_policy_is_not_wired_into_any_workflow() -> None:
    app_root = Path(policy_module.__file__).resolve().parents[2]
    importers = [
        path
        for path in app_root.rglob("*.py")
        if path.resolve() != Path(policy_module.__file__).resolve()
        and "resale_product_eligibility" in path.read_text(encoding="utf-8")
    ]
    assert importers == []
