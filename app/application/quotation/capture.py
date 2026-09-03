from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.application.quotation.contracts import QuotationScenarioLine, QuotationScenarioSnapshot
from app.application.quotation.source import QuotationScenarioSource
from app.application.workbench.exceptions import WorkbenchContractError


class QuotationScenarioSourceReader(Protocol):
    def get_scenario(self, *, scenario_id: str, company_id: int) -> QuotationScenarioSource:
        pass


class QuotationProductVariant(Protocol):
    id: int
    active: bool
    company_id: int | None


class QuotationProductVariantReader(Protocol):
    def find_by_ids(self, ids: Sequence[int]) -> Sequence[QuotationProductVariant]:
        pass


@dataclass(frozen=True, slots=True)
class CaptureQuotationScenarioCommand:
    review_id: str
    decision_id: str
    decision_version: int
    company_id: int
    scenario_id: str


class CaptureQuotationScenarioUseCase:
    def __init__(
        self,
        *,
        source_reader: QuotationScenarioSourceReader,
        product_variant_reader: QuotationProductVariantReader,
    ) -> None:
        self._source_reader = source_reader
        self._product_variant_reader = product_variant_reader

    def execute(self, command: CaptureQuotationScenarioCommand) -> QuotationScenarioSnapshot:
        source = self._source_reader.get_scenario(scenario_id=command.scenario_id, company_id=command.company_id)
        if source.company_id != command.company_id:
            raise WorkbenchContractError("quotation scenario company_id must match capture command.")
        if source.selected is not True:
            raise WorkbenchContractError("quotation scenario must be selected to be captured.")
        self._validate_product_variants(source)
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

    def _validate_product_variants(self, source: QuotationScenarioSource) -> None:
        requested_ids = tuple(sorted({line.product_variant_id for line in source.lines}))
        products = self._product_variant_reader.find_by_ids(requested_ids)

        by_id: dict[int, QuotationProductVariant] = {}
        for product in products:
            if type(product.id) is not int or product.id <= 0:
                raise WorkbenchContractError("quotation scenario product variant validation failed.")
            if product.id not in requested_ids or product.id in by_id:
                raise WorkbenchContractError("quotation scenario product variant validation failed.")
            by_id[product.id] = product

        if set(by_id) != set(requested_ids):
            raise WorkbenchContractError("quotation scenario product variant validation failed.")

        for product in by_id.values():
            if product.active is not True:
                raise WorkbenchContractError("quotation scenario product variant must be active.")
            if product.company_id is not None and product.company_id != source.company_id:
                raise WorkbenchContractError("quotation scenario product variant company_id must match scenario.")
