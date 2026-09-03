from decimal import Decimal
from typing import Any

import pytest

from app.application.workbench.exceptions import (
    WorkbenchCandidateAmbiguityError,
    WorkbenchCandidateDataError,
    WorkbenchCandidateNotFoundError,
)
from app.erp.odoo.quotation_scenario_source_reader import (
    OdooQuotationScenarioSourceFieldMapping,
    OdooQuotationScenarioSourceReader,
)


def _mapping(**overrides: object) -> OdooQuotationScenarioSourceFieldMapping:
    values: dict[str, object] = {
        "scenario_model": "x_ipp_proposal_scenario",
        "scenario_id": "x_studio_scenario_id",
        "scenario_name": "x_name",
        "scenario_selected": "x_studio_selected",
        "scenario_parent_id": "x_studio_rfq_id",
        "line_model": "x_ipp_proposal_scenario_line",
        "line_parent_id": "x_studio_scenario_id",
        "line_id": "x_studio_line_id",
        "line_product_variant_id": "x_studio_product_variant_id",
        "line_quantity": "x_studio_quantity",
        "line_uom_id": "x_studio_uom_id",
        "line_sales_unit_price": "x_studio_sales_unit_price",
        "line_cost_unit_price": "x_studio_cost_unit_price",
        "line_description": "x_studio_description",
        "line_sequence": "x_studio_sequence",
        "parent_model": "x_ipp_rfq",
        "parent_company_id": "x_studio_company_id",
        "parent_customer_id": "x_studio_customer_id",
        "parent_opportunity_id": "x_studio_opportunity_id",
        "parent_currency": "x_studio_currency",
    }
    values.update(overrides)
    return OdooQuotationScenarioSourceFieldMapping(**values)


def _scenario(**overrides: object) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": 50,
        "x_studio_scenario_id": "scenario-1",
        "x_name": "Scenario A",
        "x_studio_selected": True,
        "x_studio_rfq_id": [7, "RFQ"],
    }
    values.update(overrides)
    return values


def _parent(**overrides: object) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": 7,
        "x_studio_company_id": [1, "Company"],
        "x_studio_customer_id": [20, "Customer"],
        "x_studio_opportunity_id": [30, "Opportunity"],
        "x_studio_currency": "try",
    }
    values.update(overrides)
    return values


def _line(record_id: int, line_id: str, product_id: int, sequence: object) -> dict[str, Any]:
    return {
        "id": record_id,
        "x_studio_line_id": line_id,
        "x_studio_product_variant_id": [product_id, f"Product {product_id}"],
        "x_studio_quantity": 2,
        "x_studio_uom_id": [1, "Unit"],
        "x_studio_sales_unit_price": 0,
        "x_studio_cost_unit_price": False,
        "x_studio_description": False,
        "x_studio_sequence": sequence,
    }


class FakeAdapter:
    def __init__(
        self,
        *,
        scenarios: tuple[dict[str, Any], ...] = (_scenario(),),
        parents: tuple[dict[str, Any], ...] = (_parent(),),
        lines: tuple[dict[str, Any], ...] | None = None,
    ) -> None:
        self.scenarios = scenarios
        self.parents = parents
        self.lines = lines or (
            _line(12, "line-2", 102, 2),
            _line(11, "line-1", 101, 1),
        )
        self.reads: list[tuple[str, list[list[object]], list[str], int]] = []
        self.write_like_calls: list[str] = []

    def search_read(self, *, model, domain, fields, limit, offset=0):
        del offset
        self.reads.append((model, domain, fields, limit))
        if model == "x_ipp_proposal_scenario":
            return self.scenarios
        return self.parents

    def search_read_all(self, *, model, domain, fields, **kwargs):
        del kwargs
        self.reads.append((model, domain, fields, 0))
        return self.lines

    def create(self, *args, **kwargs):
        del args, kwargs
        self.write_like_calls.append("create")
        raise AssertionError("create must not be used")

    def write(self, *args, **kwargs):
        del args, kwargs
        self.write_like_calls.append("write")
        raise AssertionError("write must not be used")

    def unlink(self, *args, **kwargs):
        del args, kwargs
        self.write_like_calls.append("unlink")
        raise AssertionError("unlink must not be used")


def _read(adapter: FakeAdapter, mapping: OdooQuotationScenarioSourceFieldMapping | None = None):
    return OdooQuotationScenarioSourceReader(adapter=adapter, mapping=mapping or _mapping()).get_scenario(
        scenario_id="scenario-1",
        company_id=1,
    )


def test_reader_reads_valid_source_and_orders_by_sequence_then_odoo_id() -> None:
    adapter = FakeAdapter()
    source = _read(adapter)

    assert adapter.reads[0][0] == "x_ipp_proposal_scenario"
    assert adapter.reads[0][1] == [["x_studio_scenario_id", "=", "scenario-1"]]
    assert adapter.reads[0][3] == 2
    assert adapter.reads[1][1] == [["id", "=", 7]]
    assert adapter.reads[2][1] == [["x_studio_scenario_id", "=", 50]]
    assert [line.line_id for line in source.lines] == ["line-1", "line-2"]
    assert source.currency == "TRY"
    assert source.lines[0].product_variant_id == 101
    assert source.lines[0].sales_unit_price == Decimal("0")


def test_reader_rejects_scenario_not_found() -> None:
    with pytest.raises(WorkbenchCandidateNotFoundError):
        _read(FakeAdapter(scenarios=()))


def test_reader_rejects_ambiguous_scenario_lookup() -> None:
    with pytest.raises(WorkbenchCandidateAmbiguityError):
        _read(FakeAdapter(scenarios=(_scenario(), _scenario(id=51))))


def test_reader_rejects_malformed_parent_relation() -> None:
    with pytest.raises(WorkbenchCandidateDataError):
        _read(FakeAdapter(scenarios=(_scenario(x_studio_rfq_id="not-a-relation"),)))


def test_reader_rejects_malformed_product_relation() -> None:
    bad_line = _line(11, "line-1", 101, 1)
    bad_line["x_studio_product_variant_id"] = ["101", "Product"]

    with pytest.raises(WorkbenchCandidateDataError):
        _read(FakeAdapter(lines=(bad_line,)))


@pytest.mark.parametrize("currency", ["TR", "TRYX", "12Y", False, None])
def test_reader_rejects_invalid_or_malformed_currency(currency: object) -> None:
    with pytest.raises(WorkbenchCandidateDataError):
        _read(FakeAdapter(parents=(_parent(x_studio_currency=currency),)))


def test_reader_rejects_missing_required_customer_relation() -> None:
    with pytest.raises(WorkbenchCandidateDataError):
        _read(FakeAdapter(parents=(_parent(x_studio_customer_id=False),)))


def test_reader_uses_odoo_id_as_tie_breaker_for_equal_sequence_values() -> None:
    source = _read(
        FakeAdapter(
            lines=(
                _line(12, "line-2", 102, 1),
                _line(11, "line-1", 101, 1),
            )
        )
    )

    assert [line.line_id for line in source.lines] == ["line-1", "line-2"]


def test_reader_uses_odoo_id_order_when_sequence_mapping_is_absent() -> None:
    source = _read(
        FakeAdapter(
            lines=(
                _line(12, "line-2", 102, False),
                _line(11, "line-1", 101, False),
            )
        ),
        mapping=_mapping(line_sequence=None),
    )

    assert [line.line_id for line in source.lines] == ["line-1", "line-2"]
    assert [line.sequence for line in source.lines] == [None, None]


def test_reader_maps_optional_absent_or_false_values_to_none() -> None:
    line = _line(11, "line-1", 101, 1)
    del line["x_studio_cost_unit_price"]
    del line["x_studio_description"]

    source = _read(FakeAdapter(parents=(_parent(x_studio_opportunity_id=False),), lines=(line,)))

    assert source.opportunity_id is None
    assert source.lines[0].cost_unit_price is None
    assert source.lines[0].description is None


def test_reader_rejects_malformed_boolean_selection() -> None:
    with pytest.raises(WorkbenchCandidateDataError):
        _read(FakeAdapter(scenarios=(_scenario(x_studio_selected="true"),)))


def test_reader_does_not_use_write_like_methods() -> None:
    adapter = FakeAdapter()

    _read(adapter)

    assert adapter.write_like_calls == []
