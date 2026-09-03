from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.application.quotation.contracts import QuotationScenarioLine, QuotationScenarioSnapshot
from app.application.quotation.source import QuotationScenarioSource
from app.application.workbench.exceptions import WorkbenchContractError


class QuotationScenarioSourceReader(Protocol):
    def get_scenario(self, *, scenario_id: str, company_id: int) -> QuotationScenarioSource:
        pass


@dataclass(frozen=True, slots=True)
class CaptureQuotationScenarioCommand:
    review_id: str
    decision_id: str
    decision_version: int
    company_id: int
    scenario_id: str


class CaptureQuotationScenarioUseCase:
    def __init__(self, *, source_reader: QuotationScenarioSourceReader) -> None:
        self._source_reader = source_reader

    def execute(self, command: CaptureQuotationScenarioCommand) -> QuotationScenarioSnapshot:
        source = self._source_reader.get_scenario(scenario_id=command.scenario_id, company_id=command.company_id)
        if source.company_id != command.company_id:
            raise WorkbenchContractError("quotation scenario company_id must match capture command.")
        if source.selected is not True:
            raise WorkbenchContractError("quotation scenario must be selected to be captured.")
        return QuotationScenarioSnapshot(
            scenario_id=source.scenario_id,
            scenario_name=source.scenario_name,
            company_id=source.company_id,
            customer_id=source.customer_id,
            opportunity_id=source.opportunity_id,
            currency=source.currency,
            lines=tuple(
                QuotationScenarioLine(
                    line_id=line.line_id,
                    product_variant_id=line.product_variant_id,
                    quantity=line.quantity,
                    sales_unit_price=line.sales_unit_price,
                    cost_unit_price=line.cost_unit_price,
                    description=line.description,
                    uom_id=line.uom_id,
                )
                for line in source.lines
            ),
            review_id=command.review_id,
            decision_id=command.decision_id,
            decision_version=command.decision_version,
        )
