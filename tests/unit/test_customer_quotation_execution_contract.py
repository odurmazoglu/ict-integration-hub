from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.quotation import (
    CreateCustomerQuotationCommand,
    CustomerQuotationCreationResult,
    CustomerQuotationDraft,
    CustomerQuotationLine,
    QuotationScenarioLine,
    QuotationScenarioSnapshot,
    customer_quotation_execution_key,
    quotation_scenario_execution_key,
)
from app.application.quotation.contracts import CreateQuotationScenarioCommand
from app.application.workbench.exceptions import WorkbenchContractError


def _line(
    line_id: str = "line-1",
    *,
    product_variant_id: int = 10,
    quantity: str = "2",
    sales_unit_price: str = "10.00",
    description: str | None = "Widget",
    uom_id: int | None = 1,
) -> CustomerQuotationLine:
    return CustomerQuotationLine(
        line_id=line_id,
        product_variant_id=product_variant_id,
        quantity=Decimal(quantity),
        sales_unit_price=Decimal(sales_unit_price),
        description=description,
        uom_id=uom_id,
    )


def _draft(*lines: CustomerQuotationLine, **overrides: object) -> CustomerQuotationDraft:
    values: dict[str, object] = {
        "company_id": 7,
        "customer_id": 501,
        "currency": "eur",
        "scenario_id": "scenario-a",
        "scenario_name": "Scenario A",
        "review_id": "review-1",
        "decision_id": "decision-1",
        "decision_version": 4,
        "lines": lines or (_line(),),
        "opportunity_id": 88,
    }
    values.update(overrides)
    return CustomerQuotationDraft(**values)


def _snapshot(*lines: QuotationScenarioLine, **overrides: object) -> QuotationScenarioSnapshot:
    values: dict[str, object] = {
        "scenario_id": "scenario-a",
        "scenario_name": "Scenario A",
        "company_id": 7,
        "customer_id": 501,
        "currency": "eur",
        "lines": lines
        or (
            QuotationScenarioLine(
                line_id="line-1",
                product_variant_id=10,
                quantity=Decimal("2"),
                sales_unit_price=Decimal("10.00"),
                cost_unit_price=Decimal("6.00"),
                description="Widget",
                uom_id=1,
            ),
        ),
        "review_id": "review-1",
        "decision_id": "decision-1",
        "decision_version": 4,
        "opportunity_id": 88,
    }
    values.update(overrides)
    return QuotationScenarioSnapshot(**values)


def test_valid_customer_quotation_draft_from_fields_and_from_snapshot() -> None:
    draft = _draft()
    assert draft.currency == "EUR"
    assert draft.lines[0].line_id == "line-1"

    from_snapshot = CustomerQuotationDraft.from_snapshot(_snapshot())
    assert from_snapshot.company_id == 7
    assert from_snapshot.customer_id == 501
    assert from_snapshot.currency == "EUR"
    assert from_snapshot.scenario_id == "scenario-a"
    assert from_snapshot.opportunity_id == 88
    assert tuple(line.line_id for line in from_snapshot.lines) == ("line-1",)


def test_from_snapshot_does_not_carry_cost_unit_price() -> None:
    snapshot = _snapshot()
    draft = CustomerQuotationDraft.from_snapshot(snapshot)

    assert not hasattr(draft.lines[0], "cost_unit_price")
    assert "cost_unit_price" not in {field for field in CustomerQuotationLine.__dataclass_fields__}


def test_customer_quotation_draft_preserves_line_order() -> None:
    ordered = (_line("line-a"), _line("line-b"), _line("line-c"))
    draft = _draft(*ordered)
    assert tuple(line.line_id for line in draft.lines) == ("line-a", "line-b", "line-c")

    snapshot = _snapshot(
        QuotationScenarioLine(
            line_id="s-a", product_variant_id=1, quantity=Decimal("1"), sales_unit_price=Decimal("1")
        ),
        QuotationScenarioLine(
            line_id="s-b", product_variant_id=2, quantity=Decimal("1"), sales_unit_price=Decimal("1")
        ),
    )
    assert tuple(line.line_id for line in CustomerQuotationDraft.from_snapshot(snapshot).lines) == ("s-a", "s-b")


def test_customer_quotation_line_requires_canonical_decimal() -> None:
    with pytest.raises(WorkbenchContractError):
        CustomerQuotationLine(line_id="l", product_variant_id=1, quantity=2.0, sales_unit_price=Decimal("1"))  # type: ignore[arg-type]
    with pytest.raises(WorkbenchContractError):
        CustomerQuotationLine(line_id="l", product_variant_id=1, quantity=Decimal("1"), sales_unit_price=1.0)  # type: ignore[arg-type]


def test_customer_quotation_draft_rejects_duplicate_line_ids() -> None:
    with pytest.raises(WorkbenchContractError):
        _draft(_line("dup"), _line("dup"))


def test_customer_quotation_line_requires_positive_quantity() -> None:
    for value in ("0", "-1"):
        with pytest.raises(WorkbenchContractError):
            _line(quantity=value)


def test_customer_quotation_line_allows_zero_but_not_negative_sales_price() -> None:
    assert _line(sales_unit_price="0").sales_unit_price == Decimal("0")
    with pytest.raises(WorkbenchContractError):
        _line(sales_unit_price="-0.01")


def test_customer_quotation_line_optional_uom_and_description() -> None:
    line = _line(uom_id=None, description=None)
    assert line.uom_id is None
    assert line.description is None


def test_customer_quotation_draft_is_immutable() -> None:
    draft = _draft()
    with pytest.raises(FrozenInstanceError):
        draft.company_id = 9  # type: ignore[misc]


def test_scenario_execution_key_is_deterministic_and_identity_scoped() -> None:
    key_a = customer_quotation_execution_key(
        company_id=7, review_id="review-1", decision_id="decision-1", decision_version=4, scenario_id="scenario-a"
    )
    key_a_again = customer_quotation_execution_key(
        company_id=7, review_id="review-1", decision_id="decision-1", decision_version=4, scenario_id="scenario-a"
    )
    key_b = customer_quotation_execution_key(
        company_id=7, review_id="review-1", decision_id="decision-1", decision_version=4, scenario_id="scenario-b"
    )

    assert key_a == key_a_again
    assert key_a != key_b
    assert key_a.startswith("quotation-scenario-execution:")

    # The draft key is a pure function of the immutable scenario identity: changing
    # the scenario name, prices, or line contents must not change the key.
    base = _draft()
    mutated = _draft(_line(sales_unit_price="999.99"), scenario_name="Renamed Scenario")
    assert base.execution_key == mutated.execution_key == key_a


def test_draft_execution_key_matches_legacy_scenario_execution_key() -> None:
    snapshot = _snapshot()
    draft = CustomerQuotationDraft.from_snapshot(snapshot)
    legacy = quotation_scenario_execution_key(
        CreateQuotationScenarioCommand(
            review_id=snapshot.review_id,
            company_id=snapshot.company_id,
            decision_id=snapshot.decision_id,
            decision_version=snapshot.decision_version,
            scenario=snapshot,
        )
    )
    assert draft.execution_key == legacy


def test_create_customer_quotation_command_validates_draft_and_approver() -> None:
    command = CreateCustomerQuotationCommand(draft=_draft(), approved_by="controller")
    assert command.execution_key == _draft().execution_key
    with pytest.raises(WorkbenchContractError):
        CreateCustomerQuotationCommand(draft=object())  # type: ignore[arg-type]
    with pytest.raises(WorkbenchContractError):
        CreateCustomerQuotationCommand(draft=_draft(), approved_by="   ")


def test_customer_quotation_creation_result_contract() -> None:
    result = CustomerQuotationCreationResult(
        external_quotation_id=8001,
        execution_key="quotation-scenario-execution:abc",
        created=True,
        external_reference="S00042",
    )
    assert result.created is True
    with pytest.raises(WorkbenchContractError):
        CustomerQuotationCreationResult(external_quotation_id=0, execution_key="k", created=True)
    with pytest.raises(WorkbenchContractError):
        CustomerQuotationCreationResult(external_quotation_id=1, execution_key="", created=True)


def test_execution_contract_module_has_no_infra_or_odoo_imports() -> None:
    tree = ast.parse(Path("app/application/quotation/execution.py").read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)

    assert not any(
        module.startswith(("sqlalchemy", "app.models", "app.erp", "app.connectors", "app.composition"))
        for module in modules
    )

    body = list(tree.body)
    if body and isinstance(body[0], ast.Expr):
        body = body[1:]
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            node.value.value = ""
    code = ast.unparse(ast.Module(body=body, type_ignores=[])).lower()
    for token in ("sale.order", "json2", "search_read", "create_sale_order", "httpx"):
        assert token not in code
