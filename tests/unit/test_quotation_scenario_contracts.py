from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from app.application.quotation import (
    CreateQuotationScenarioCommand,
    QuotationScenarioLine,
    QuotationScenarioSnapshot,
    quotation_scenario_execution_key,
)
from app.application.workbench.exceptions import WorkbenchContractError


def _line(line_id: str = "line-1", *, price: str = "10.00") -> QuotationScenarioLine:
    return QuotationScenarioLine(
        line_id=line_id,
        product_variant_id=10,
        quantity=Decimal("2"),
        sales_unit_price=Decimal(price),
        cost_unit_price=Decimal("6.00"),
        description="Product",
        uom_id=1,
    )


def _snapshot(*lines: QuotationScenarioLine, **overrides: object) -> QuotationScenarioSnapshot:
    values: dict[str, object] = {
        "scenario_id": "scenario-a",
        "scenario_name": "Scenario A",
        "company_id": 1,
        "customer_id": 20,
        "currency": "try",
        "lines": lines or (_line(),),
        "review_id": "review-1",
        "decision_id": "decision-1",
        "decision_version": 2,
    }
    values.update(overrides)
    return QuotationScenarioSnapshot(**values)


def _command(snapshot: QuotationScenarioSnapshot | None = None, **overrides: object) -> CreateQuotationScenarioCommand:
    values: dict[str, object] = {
        "review_id": "review-1",
        "company_id": 1,
        "decision_id": "decision-1",
        "decision_version": 2,
        "scenario": snapshot or _snapshot(),
    }
    values.update(overrides)
    return CreateQuotationScenarioCommand(**values)


def test_valid_scenario_preserves_decimal_prices_and_line_order() -> None:
    first = _line("line-1", price="0")
    second = _line("line-2", price="12.345")

    snapshot = _snapshot(first, second)

    assert snapshot.lines == (first, second)
    assert snapshot.currency == "TRY"
    assert snapshot.lines[0].sales_unit_price == Decimal("0")
    assert snapshot.lines[0].cost_unit_price == Decimal("6.00")


def test_dtos_are_frozen() -> None:
    line = _line()

    with pytest.raises(FrozenInstanceError):
        line.quantity = Decimal("3")  # type: ignore[misc]


@pytest.mark.parametrize(
    ("factory", "kwargs"),
    [
        (
            QuotationScenarioLine,
            {"line_id": "", "product_variant_id": 10, "quantity": Decimal("1"), "sales_unit_price": Decimal("1")},
        ),
        (
            QuotationScenarioLine,
            {"line_id": "l", "product_variant_id": 0, "quantity": Decimal("1"), "sales_unit_price": Decimal("1")},
        ),
        (
            QuotationScenarioLine,
            {"line_id": "l", "product_variant_id": 10, "quantity": Decimal("0"), "sales_unit_price": Decimal("1")},
        ),
        (
            QuotationScenarioLine,
            {"line_id": "l", "product_variant_id": 10, "quantity": Decimal("1"), "sales_unit_price": Decimal("-1")},
        ),
        (
            QuotationScenarioSnapshot,
            {
                "scenario_id": "",
                "scenario_name": "A",
                "company_id": 1,
                "customer_id": 2,
                "currency": "TRY",
                "lines": (_line(),),
                "review_id": "r",
                "decision_id": "d",
                "decision_version": 1,
            },
        ),
        (
            QuotationScenarioSnapshot,
            {
                "scenario_id": "s",
                "scenario_name": "A",
                "company_id": 1,
                "customer_id": 2,
                "currency": "TRY",
                "lines": (),
                "review_id": "r",
                "decision_id": "d",
                "decision_version": 1,
            },
        ),
        (
            QuotationScenarioSnapshot,
            {
                "scenario_id": "s",
                "scenario_name": "A",
                "company_id": 1,
                "customer_id": 2,
                "currency": "TRY",
                "lines": (_line("x"), _line("x")),
                "review_id": "r",
                "decision_id": "d",
                "decision_version": 1,
            },
        ),
        (
            QuotationScenarioSnapshot,
            {
                "scenario_id": "s",
                "scenario_name": "A",
                "company_id": 1,
                "customer_id": 2,
                "currency": "TRY",
                "lines": (_line(),),
                "review_id": "r",
                "decision_id": "d",
                "decision_version": 1,
                "selected": False,
            },
        ),
    ],
)
def test_invalid_scenario_values_fail_closed(factory: type[object], kwargs: dict[str, object]) -> None:
    with pytest.raises(WorkbenchContractError):
        factory(**kwargs)


def test_command_requires_matching_company_and_context_identity() -> None:
    with pytest.raises(WorkbenchContractError):
        _command(company_id=2)

    with pytest.raises(WorkbenchContractError):
        _command(decision_version=3)


def test_identity_is_deterministic_and_excludes_mutable_values() -> None:
    command = _command()
    changed_label = _command(_snapshot(scenario_name="Changed", lines=(_line(price="999"),)))
    reordered = _command(_snapshot(lines=(_line("line-2"), _line("line-1"))))

    assert quotation_scenario_execution_key(command) == quotation_scenario_execution_key(command)
    assert quotation_scenario_execution_key(command) == quotation_scenario_execution_key(changed_label)
    assert quotation_scenario_execution_key(command) == quotation_scenario_execution_key(reordered)
    assert "2026" not in quotation_scenario_execution_key(command)


def test_identity_changes_for_scenario_or_decision_version() -> None:
    command = _command()

    assert quotation_scenario_execution_key(command) != quotation_scenario_execution_key(
        _command(_snapshot(scenario_id="scenario-b"))
    )
    assert quotation_scenario_execution_key(command) != quotation_scenario_execution_key(
        _command(decision_version=3, scenario=_snapshot(decision_version=3))
    )
