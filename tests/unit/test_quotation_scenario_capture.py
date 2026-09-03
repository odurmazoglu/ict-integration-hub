from decimal import Decimal

import pytest

from app.application.quotation.capture import CaptureQuotationScenarioCommand, CaptureQuotationScenarioUseCase
from app.application.quotation.source import QuotationScenarioSource, QuotationScenarioSourceLine
from app.application.workbench.exceptions import WorkbenchContractError


def _source(*, selected: bool = True, company_id: int = 1, currency: str = "try") -> QuotationScenarioSource:
    return QuotationScenarioSource(
        scenario_id="scenario-1",
        scenario_name="Scenario A",
        selected=selected,
        company_id=company_id,
        customer_id=20,
        opportunity_id=30,
        currency=currency,
        lines=(
            QuotationScenarioSourceLine(
                line_id="line-2",
                product_variant_id=102,
                quantity=Decimal("2"),
                sales_unit_price=Decimal("0"),
                cost_unit_price=Decimal("4.50"),
                description="Second",
                uom_id=1,
                sequence=Decimal("2"),
            ),
            QuotationScenarioSourceLine(
                line_id="line-1",
                product_variant_id=101,
                quantity=Decimal("1.5"),
                sales_unit_price=Decimal("10.25"),
                cost_unit_price=None,
                description="First",
                uom_id=1,
                sequence=Decimal("1"),
            ),
        ),
    )


class FakeReader:
    def __init__(self, source: QuotationScenarioSource) -> None:
        self.source = source
        self.calls: list[tuple[str, int]] = []

    def get_scenario(self, *, scenario_id: str, company_id: int) -> QuotationScenarioSource:
        self.calls.append((scenario_id, company_id))
        return self.source


def _command(**overrides: object) -> CaptureQuotationScenarioCommand:
    values: dict[str, object] = {
        "review_id": "review-1",
        "decision_id": "decision-1",
        "decision_version": 2,
        "company_id": 1,
        "scenario_id": "scenario-1",
    }
    values.update(overrides)
    return CaptureQuotationScenarioCommand(**values)


def test_capture_preserves_source_values_and_order() -> None:
    reader = FakeReader(_source())
    snapshot = CaptureQuotationScenarioUseCase(source_reader=reader).execute(_command())

    assert reader.calls == [("scenario-1", 1)]
    assert snapshot.currency == "TRY"
    assert snapshot.customer_id == 20
    assert snapshot.opportunity_id == 30
    assert [line.line_id for line in snapshot.lines] == ["line-2", "line-1"]
    assert snapshot.lines[0].sales_unit_price == Decimal("0")
    assert snapshot.lines[0].cost_unit_price == Decimal("4.50")
    assert snapshot.lines[1].sales_unit_price == Decimal("10.25")


@pytest.mark.parametrize(
    "source",
    [_source(selected=False), _source(company_id=2)],
)
def test_capture_rejects_invalid_source_context(source: QuotationScenarioSource) -> None:
    with pytest.raises(WorkbenchContractError):
        CaptureQuotationScenarioUseCase(source_reader=FakeReader(source)).execute(_command())
