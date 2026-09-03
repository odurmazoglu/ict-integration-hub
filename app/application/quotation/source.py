from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.application.dto import ApplicationDTO


@dataclass(frozen=True, slots=True)
class QuotationScenarioSourceLine(ApplicationDTO):
    line_id: str
    product_variant_id: int
    quantity: Decimal
    sales_unit_price: Decimal
    cost_unit_price: Decimal | None
    description: str | None
    uom_id: int | None
    sequence: Decimal | None


@dataclass(frozen=True, slots=True)
class QuotationScenarioSource(ApplicationDTO):
    scenario_id: str
    scenario_name: str
    selected: bool
    company_id: int
    customer_id: int
    opportunity_id: int | None
    currency: str
    lines: tuple[QuotationScenarioSourceLine, ...]
