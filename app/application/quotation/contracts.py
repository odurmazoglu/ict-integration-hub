from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import WorkbenchContractError


@dataclass(frozen=True, slots=True)
class QuotationScenarioLine(ApplicationDTO):
    """Immutable ERP-independent line in a customer quotation scenario."""

    line_id: str
    product_variant_id: int
    quantity: Decimal
    sales_unit_price: Decimal
    cost_unit_price: Decimal | None = None
    description: str | None = None
    uom_id: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.line_id, "line_id is required.")
        _require_positive_int(self.product_variant_id, "product_variant_id must be positive.")
        _require_positive_decimal(self.quantity, "quantity must be greater than zero.")
        _require_nonnegative_decimal(self.sales_unit_price, "sales_unit_price must not be negative.")
        if self.cost_unit_price is not None:
            _require_nonnegative_decimal(self.cost_unit_price, "cost_unit_price must not be negative.")
        if self.description is not None and not isinstance(self.description, str):
            raise WorkbenchContractError("description must be a string when supplied.")
        if self.uom_id is not None:
            _require_positive_int(self.uom_id, "uom_id must be positive when supplied.")


@dataclass(frozen=True, slots=True)
class QuotationScenarioSnapshot(ApplicationDTO):
    """Immutable captured source snapshot for one selected customer quotation."""

    scenario_id: str
    scenario_name: str
    company_id: int
    customer_id: int
    currency: str
    lines: tuple[QuotationScenarioLine, ...]
    review_id: str
    decision_id: str
    decision_version: int
    opportunity_id: int | None = None
    selected: bool = True

    def __post_init__(self) -> None:
        _require_text(self.scenario_id, "scenario_id is required.")
        _require_text(self.scenario_name, "scenario_name is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.customer_id, "customer_id must be positive.")
        _require_currency(self.currency)
        _require_text(self.review_id, "review_id is required.")
        _require_text(self.decision_id, "decision_id is required.")
        _require_positive_int(self.decision_version, "decision_version must be positive.")
        if self.opportunity_id is not None:
            _require_positive_int(self.opportunity_id, "opportunity_id must be positive when supplied.")
        if self.selected is not True:
            raise WorkbenchContractError("quotation scenario must be selected to be executable.")
        lines = tuple(self.lines)
        if not lines:
            raise WorkbenchContractError("quotation scenario requires at least one line.")
        if any(not isinstance(line, QuotationScenarioLine) for line in lines):
            raise WorkbenchContractError("quotation scenario lines must be canonical.")
        if len({line.line_id for line in lines}) != len(lines):
            raise WorkbenchContractError("quotation scenario line_id values must be unique.")
        object.__setattr__(self, "lines", lines)
        object.__setattr__(self, "currency", self.currency.strip().upper())


@dataclass(frozen=True, slots=True)
class CreateQuotationScenarioCommand(ApplicationDTO):
    """Command to prepare one captured scenario for future quotation execution."""

    review_id: str
    company_id: int
    decision_id: str
    decision_version: int
    scenario: QuotationScenarioSnapshot

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_text(self.decision_id, "decision_id is required.")
        _require_positive_int(self.decision_version, "decision_version must be positive.")
        if not isinstance(self.scenario, QuotationScenarioSnapshot):
            raise WorkbenchContractError("scenario must be a canonical QuotationScenarioSnapshot.")
        if self.scenario.review_id != self.review_id:
            raise WorkbenchContractError("scenario review_id must match command review_id.")
        if self.scenario.company_id != self.company_id:
            raise WorkbenchContractError("scenario company_id must match command company_id.")
        if self.scenario.decision_id != self.decision_id:
            raise WorkbenchContractError("scenario decision_id must match command decision_id.")
        if self.scenario.decision_version != self.decision_version:
            raise WorkbenchContractError("scenario decision_version must match command decision_version.")


def _require_text(value: object, message: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(message)


def _require_positive_int(value: object, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)


def _require_currency(value: object) -> None:
    if not isinstance(value, str) or len(value.strip()) != 3 or not value.strip().isalpha():
        raise WorkbenchContractError("currency must be a three-letter code.")


def _require_decimal(value: object, message: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise WorkbenchContractError(message)
    return value


def _require_positive_decimal(value: object, message: str) -> None:
    if _require_decimal(value, message) <= Decimal("0"):
        raise WorkbenchContractError(message)


def _require_nonnegative_decimal(value: object, message: str) -> None:
    if _require_decimal(value, message) < Decimal("0"):
        raise WorkbenchContractError(message)
