from decimal import Decimal

from app.erp.odoo.quotation_scenario_source_reader import (
    OdooQuotationScenarioSourceFieldMapping,
    OdooQuotationScenarioSourceReader,
)


def _mapping() -> OdooQuotationScenarioSourceFieldMapping:
    return OdooQuotationScenarioSourceFieldMapping(
        scenario_model="x_ipp_proposal_scenario",
        scenario_id="x_studio_scenario_id",
        scenario_name="x_name",
        scenario_selected="x_studio_selected",
        scenario_parent_id="x_studio_rfq_id",
        line_model="x_ipp_proposal_scenario_line",
        line_parent_id="x_studio_scenario_id",
        line_id="x_studio_line_id",
        line_product_variant_id="x_studio_product_variant_id",
        line_quantity="x_studio_quantity",
        line_uom_id="x_studio_uom_id",
        line_sales_unit_price="x_studio_sales_unit_price",
        line_cost_unit_price="x_studio_cost_unit_price",
        line_description="x_studio_description",
        line_sequence="x_studio_sequence",
        parent_model="x_ipp_rfq",
        parent_company_id="x_studio_company_id",
        parent_customer_id="x_studio_customer_id",
        parent_opportunity_id="x_studio_opportunity_id",
        parent_currency="x_studio_currency",
    )


class FakeAdapter:
    def __init__(self) -> None:
        self.reads: list[tuple[str, list[list[object]], list[str], int]] = []

    def search_read(self, *, model, domain, fields, limit, offset=0):
        self.reads.append((model, domain, fields, limit))
        if model == "x_ipp_proposal_scenario":
            return (
                {
                    "id": 50,
                    "x_studio_scenario_id": "scenario-1",
                    "x_name": "Scenario A",
                    "x_studio_selected": True,
                    "x_studio_rfq_id": [7, "RFQ"],
                },
            )
        return (
            {
                "id": 7,
                "x_studio_company_id": [1, "Company"],
                "x_studio_customer_id": [20, "Customer"],
                "x_studio_opportunity_id": [30, "Opportunity"],
                "x_studio_currency": "try",
            },
        )

    def search_read_all(self, *, model, domain, fields, **kwargs):
        self.reads.append((model, domain, fields, 0))
        return (
            {
                "id": 12,
                "x_studio_line_id": "line-2",
                "x_studio_product_variant_id": [102, "Product 2"],
                "x_studio_quantity": 2,
                "x_studio_uom_id": [1, "Unit"],
                "x_studio_sales_unit_price": 0,
                "x_studio_cost_unit_price": 4.5,
                "x_studio_description": "Second",
                "x_studio_sequence": 2,
            },
            {
                "id": 11,
                "x_studio_line_id": "line-1",
                "x_studio_product_variant_id": [101, "Product 1"],
                "x_studio_quantity": 1,
                "x_studio_uom_id": [1, "Unit"],
                "x_studio_sales_unit_price": 10.25,
                "x_studio_cost_unit_price": False,
                "x_studio_description": "First",
                "x_studio_sequence": 1,
            },
        )


def test_reader_uses_exact_read_domains_and_deterministic_sequence_order() -> None:
    adapter = FakeAdapter()
    source = OdooQuotationScenarioSourceReader(adapter=adapter, mapping=_mapping()).get_scenario(
        scenario_id="scenario-1",
        company_id=1,
    )

    assert adapter.reads[0][0] == "x_ipp_proposal_scenario"
    assert adapter.reads[0][1] == [["x_studio_scenario_id", "=", "scenario-1"]]
    assert adapter.reads[0][3] == 2
    assert adapter.reads[1][1] == [["id", "=", 7]]
    assert adapter.reads[2][1] == [["x_studio_scenario_id", "=", 50]]
    assert [line.line_id for line in source.lines] == ["line-1", "line-2"]
    assert source.currency == "TRY"
    assert source.lines[0].sales_unit_price == Decimal("10.25")
    assert source.lines[1].sales_unit_price == Decimal("0")
