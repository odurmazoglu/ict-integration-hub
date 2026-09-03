from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.application.quotation.source import QuotationScenarioSource, QuotationScenarioSourceLine
from app.application.workbench.exceptions import (
    WorkbenchCandidateAmbiguityError,
    WorkbenchCandidateDataError,
    WorkbenchCandidateNotFoundError,
    WorkbenchCandidateReadError,
)
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.adapter import OdooReadOnlyAdapter

SAFE_QUOTATION_SOURCE_READ_ERROR = "Odoo quotation scenario source read failed."
SAFE_QUOTATION_SOURCE_DATA_ERROR = "Odoo quotation scenario source data is invalid."
SAFE_QUOTATION_SOURCE_NOT_FOUND = "Odoo quotation scenario was not found."
SAFE_QUOTATION_SOURCE_AMBIGUITY = "Odoo quotation scenario lookup returned multiple records."


@dataclass(frozen=True, slots=True)
class OdooQuotationScenarioSourceFieldMapping:
    scenario_model: str
    scenario_id: str
    scenario_name: str
    scenario_selected: str
    scenario_parent_id: str
    line_model: str
    line_parent_id: str
    line_id: str
    line_product_variant_id: str
    line_quantity: str
    line_uom_id: str | None
    line_sales_unit_price: str
    line_cost_unit_price: str | None
    line_description: str | None
    line_sequence: str | None
    parent_model: str
    parent_company_id: str
    parent_customer_id: str
    parent_opportunity_id: str | None
    parent_currency: str

    def __post_init__(self) -> None:
        required = (
            "scenario_model",
            "scenario_id",
            "scenario_name",
            "scenario_selected",
            "scenario_parent_id",
            "line_model",
            "line_parent_id",
            "line_id",
            "line_product_variant_id",
            "line_quantity",
            "line_sales_unit_price",
            "parent_model",
            "parent_company_id",
            "parent_customer_id",
            "parent_currency",
        )
        for field_name in required:
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise WorkbenchCandidateDataError(f"{field_name} mapping is required.")

    @classmethod
    def from_environment(cls, *, prefix: str = "ODOO_QUOTATION_SCENARIO_") -> OdooQuotationScenarioSourceFieldMapping:
        return cls(
            scenario_model=_env(prefix, "SCENARIO_MODEL"),
            scenario_id=_env(prefix, "SCENARIO_ID_FIELD"),
            scenario_name=_env(prefix, "SCENARIO_NAME_FIELD"),
            scenario_selected=_env(prefix, "SCENARIO_SELECTED_FIELD"),
            scenario_parent_id=_env(prefix, "SCENARIO_PARENT_FIELD"),
            line_model=_env(prefix, "LINE_MODEL"),
            line_parent_id=_env(prefix, "LINE_PARENT_FIELD"),
            line_id=_env(prefix, "LINE_ID_FIELD"),
            line_product_variant_id=_env(prefix, "LINE_PRODUCT_VARIANT_FIELD"),
            line_quantity=_env(prefix, "LINE_QUANTITY_FIELD"),
            line_uom_id=_env_optional(prefix, "LINE_UOM_FIELD"),
            line_sales_unit_price=_env(prefix, "LINE_SALES_UNIT_PRICE_FIELD"),
            line_cost_unit_price=_env_optional(prefix, "LINE_COST_UNIT_PRICE_FIELD"),
            line_description=_env_optional(prefix, "LINE_DESCRIPTION_FIELD"),
            line_sequence=_env_optional(prefix, "LINE_SEQUENCE_FIELD"),
            parent_model=_env(prefix, "PARENT_MODEL"),
            parent_company_id=_env(prefix, "PARENT_COMPANY_FIELD"),
            parent_customer_id=_env(prefix, "PARENT_CUSTOMER_FIELD"),
            parent_opportunity_id=_env_optional(prefix, "PARENT_OPPORTUNITY_FIELD"),
            parent_currency=_env(prefix, "PARENT_CURRENCY_FIELD"),
        )


class OdooQuotationScenarioSourceReader:
    def __init__(
        self,
        *,
        adapter: OdooReadOnlyAdapter,
        mapping: OdooQuotationScenarioSourceFieldMapping,
    ) -> None:
        self._adapter = adapter
        self._mapping = mapping

    def get_scenario(self, *, scenario_id: str, company_id: int) -> QuotationScenarioSource:
        try:
            scenario = self._scenario(scenario_id)
            scenario_record_id = _required_id(scenario.get("id"))
            parent_id = _required_relation(scenario.get(self._mapping.scenario_parent_id))
            parent = self._parent(parent_id)
            source = QuotationScenarioSource(
                scenario_id=_required_text(scenario.get(self._mapping.scenario_id)),
                scenario_name=_required_text(scenario.get(self._mapping.scenario_name)),
                selected=_required_bool(scenario.get(self._mapping.scenario_selected)),
                company_id=_required_relation(parent.get(self._mapping.parent_company_id)),
                customer_id=_required_relation(parent.get(self._mapping.parent_customer_id)),
                opportunity_id=(
                    _optional_relation(parent.get(self._mapping.parent_opportunity_id))
                    if self._mapping.parent_opportunity_id is not None
                    else None
                ),
                currency=_currency(parent.get(self._mapping.parent_currency)),
                lines=self._lines(scenario_record_id),
            )
            if source.scenario_id != scenario_id or source.company_id != company_id:
                raise WorkbenchCandidateDataError(SAFE_QUOTATION_SOURCE_DATA_ERROR)
            return source
        except (WorkbenchCandidateReadError, WorkbenchCandidateDataError, WorkbenchCandidateAmbiguityError):
            raise
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise WorkbenchCandidateDataError(SAFE_QUOTATION_SOURCE_DATA_ERROR) from exc
        except ErpRepositoryError as exc:
            raise WorkbenchCandidateReadError(SAFE_QUOTATION_SOURCE_READ_ERROR) from exc

    def _scenario(self, scenario_id: str) -> dict[str, Any]:
        records = self._adapter.search_read(
            model=self._mapping.scenario_model,
            domain=[[self._mapping.scenario_id, "=", scenario_id]],
            fields=_unique(
                "id",
                self._mapping.scenario_id,
                self._mapping.scenario_name,
                self._mapping.scenario_selected,
                self._mapping.scenario_parent_id,
            ),
            limit=2,
        )
        if not records:
            raise WorkbenchCandidateNotFoundError(SAFE_QUOTATION_SOURCE_NOT_FOUND)
        if len(records) > 1:
            raise WorkbenchCandidateAmbiguityError(SAFE_QUOTATION_SOURCE_AMBIGUITY)
        return records[0]

    def _parent(self, parent_id: int) -> dict[str, Any]:
        records = self._adapter.search_read(
            model=self._mapping.parent_model,
            domain=[["id", "=", parent_id]],
            fields=_unique(
                "id",
                self._mapping.parent_company_id,
                self._mapping.parent_customer_id,
                self._mapping.parent_opportunity_id,
                self._mapping.parent_currency,
            ),
            limit=2,
        )
        if not records:
            raise WorkbenchCandidateNotFoundError(SAFE_QUOTATION_SOURCE_NOT_FOUND)
        if len(records) > 1:
            raise WorkbenchCandidateAmbiguityError(SAFE_QUOTATION_SOURCE_AMBIGUITY)
        return records[0]

    def _lines(self, parent_id: int) -> tuple[QuotationScenarioSourceLine, ...]:
        records = self._adapter.search_read_all(
            model=self._mapping.line_model,
            domain=[[self._mapping.line_parent_id, "=", parent_id]],
            fields=_unique(
                "id",
                self._mapping.line_id,
                self._mapping.line_product_variant_id,
                self._mapping.line_quantity,
                self._mapping.line_uom_id,
                self._mapping.line_sales_unit_price,
                self._mapping.line_cost_unit_price,
                self._mapping.line_description,
                self._mapping.line_sequence,
            ),
        )
        ordered = sorted(
            records,
            key=lambda record: (
                _sequence(record.get(self._mapping.line_sequence)),
                _required_id(record.get("id")),
            ),
        )
        return tuple(
            QuotationScenarioSourceLine(
                line_id=_required_text(record.get(self._mapping.line_id)),
                product_variant_id=_required_relation(record.get(self._mapping.line_product_variant_id)),
                quantity=_decimal(record.get(self._mapping.line_quantity)),
                sales_unit_price=_decimal(record.get(self._mapping.line_sales_unit_price)),
                cost_unit_price=(
                    _optional_decimal(record.get(self._mapping.line_cost_unit_price))
                    if self._mapping.line_cost_unit_price is not None
                    else None
                ),
                description=(
                    _optional_text(record.get(self._mapping.line_description))
                    if self._mapping.line_description is not None
                    else None
                ),
                uom_id=(
                    _optional_relation(record.get(self._mapping.line_uom_id))
                    if self._mapping.line_uom_id is not None
                    else None
                ),
                sequence=_optional_decimal(record.get(self._mapping.line_sequence))
                if self._mapping.line_sequence is not None
                else None,
            )
            for record in ordered
        )


def _unique(*values: str | None) -> list[str]:
    return list(dict.fromkeys(value for value in values if value is not None))


def _required_id(value: Any) -> int:
    if type(value) is int and value > 0:
        return value
    raise WorkbenchCandidateDataError(SAFE_QUOTATION_SOURCE_DATA_ERROR)


def _required_relation(value: Any) -> int:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, (list, tuple)) and len(value) == 2 and type(value[0]) is int and value[0] > 0:
        return value[0]
    raise WorkbenchCandidateDataError(SAFE_QUOTATION_SOURCE_DATA_ERROR)


def _optional_relation(value: Any) -> int | None:
    if value in (None, False):
        return None
    return _required_relation(value)


def _required_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchCandidateDataError(SAFE_QUOTATION_SOURCE_DATA_ERROR)
    return value.strip()


def _optional_text(value: Any) -> str | None:
    return None if value in (None, False) else _required_text(value)


def _required_bool(value: Any) -> bool:
    if type(value) is bool:
        return value
    raise WorkbenchCandidateDataError(SAFE_QUOTATION_SOURCE_DATA_ERROR)


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise WorkbenchCandidateDataError(SAFE_QUOTATION_SOURCE_DATA_ERROR)
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise WorkbenchCandidateDataError(SAFE_QUOTATION_SOURCE_DATA_ERROR) from exc
    if not decimal.is_finite():
        raise WorkbenchCandidateDataError(SAFE_QUOTATION_SOURCE_DATA_ERROR)
    return decimal


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value in (None, False) else _decimal(value)


def _currency(value: Any) -> str:
    currency = (
        value if isinstance(value, str) else value[1] if isinstance(value, (list, tuple)) and len(value) == 2 else None
    )
    if not isinstance(currency, str) or len(currency.strip()) != 3 or not currency.strip().isalpha():
        raise WorkbenchCandidateDataError(SAFE_QUOTATION_SOURCE_DATA_ERROR)
    return currency.strip().upper()


def _sequence(value: Any) -> tuple[int, Decimal]:
    if value in (None, False):
        return (1, Decimal("0"))
    decimal = _decimal(value)
    return (0, decimal)


def _env(prefix: str, name: str) -> str:
    return os.environ.get(f"{prefix}{name}", "")


def _env_optional(prefix: str, name: str) -> str | None:
    value = _env(prefix, name)
    return value or None
