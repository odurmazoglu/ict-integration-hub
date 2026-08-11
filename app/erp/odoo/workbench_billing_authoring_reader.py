from __future__ import annotations

import math
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.application.workbench.billing_authoring import WorkbenchBillingAuthoringRow
from app.application.workbench.exceptions import (
    WorkbenchCandidateAmbiguityError,
    WorkbenchCandidateDataError,
    WorkbenchCandidateNotFoundError,
    WorkbenchCandidateReadError,
    WorkbenchContractError,
)
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.adapter import OdooReadOnlyAdapter

SAFE_BILLING_READ_ERROR = "Odoo Workbench billing authoring read failed."
SAFE_BILLING_DATA_ERROR = "Odoo Workbench billing authoring data is invalid."
SAFE_BILLING_AMBIGUITY_ERROR = "Odoo Workbench billing parent lookup returned multiple records."
SAFE_BILLING_NOT_FOUND = "Odoo Workbench billing authoring was not found."


@dataclass(frozen=True, slots=True)
class OdooWorkbenchBillingFieldMapping:
    parent_model: str
    parent_review_id: str
    parent_company_id: str
    parent_review_version: str
    billing_model: str
    parent_many2one_field: str
    billing_group_key: str
    allocation_key: str
    customer_id: str
    product_id: str
    description: str
    quantity: str
    unit_price: str
    currency_id: str
    sales_tax_ids: str
    billing_ready: str
    sequence: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "parent_model",
            "parent_review_id",
            "parent_company_id",
            "parent_review_version",
            "billing_model",
            "parent_many2one_field",
            "billing_group_key",
            "allocation_key",
            "customer_id",
            "product_id",
            "description",
            "quantity",
            "unit_price",
            "currency_id",
            "sales_tax_ids",
            "billing_ready",
        ):
            _require_mapping_text(getattr(self, field_name), f"{field_name} mapping is required.")
        if self.sequence is not None:
            _require_mapping_text(self.sequence, "sequence mapping must be non-empty when supplied.")

    @classmethod
    def from_environment(cls, *, prefix: str = "ODOO_WORKBENCH_BILLING_") -> OdooWorkbenchBillingFieldMapping:
        return cls(
            parent_model=_env(prefix, "PARENT_MODEL"),
            parent_review_id=_env(prefix, "PARENT_REVIEW_ID_FIELD"),
            parent_company_id=_env(prefix, "PARENT_COMPANY_ID_FIELD"),
            parent_review_version=_env(prefix, "PARENT_REVIEW_VERSION_FIELD"),
            billing_model=_env(prefix, "MODEL"),
            parent_many2one_field=_env(prefix, "PARENT_MANY2ONE_FIELD"),
            billing_group_key=_env(prefix, "GROUP_KEY_FIELD"),
            allocation_key=_env(prefix, "ALLOCATION_KEY_FIELD"),
            customer_id=_env(prefix, "CUSTOMER_FIELD"),
            product_id=_env(prefix, "PRODUCT_FIELD"),
            description=_env(prefix, "DESCRIPTION_FIELD"),
            quantity=_env(prefix, "QUANTITY_FIELD"),
            unit_price=_env(prefix, "UNIT_PRICE_FIELD"),
            currency_id=_env(prefix, "CURRENCY_FIELD"),
            sales_tax_ids=_env(prefix, "SALES_TAX_IDS_FIELD"),
            billing_ready=_env(prefix, "READY_FIELD"),
            sequence=_env_optional(prefix, "SEQUENCE_FIELD"),
        )


class OdooWorkbenchBillingAuthoringReader:
    """Read Odoo-authored Customer Invoice billing rows without mutating Odoo."""

    def __init__(self, *, adapter: OdooReadOnlyAdapter, mapping: OdooWorkbenchBillingFieldMapping) -> None:
        self._adapter = adapter
        self._mapping = mapping

    def get_billing_authoring(
        self,
        *,
        review_id: str,
        company_id: int,
    ) -> tuple[WorkbenchBillingAuthoringRow, ...]:
        _required_text(review_id, "review_id is required.")
        if type(company_id) is not int or company_id <= 0:
            raise WorkbenchContractError("company_id must be positive.")
        try:
            parent = self._parent_record(review_id=review_id, company_id=company_id)
            parent_id = _required_many2one_id(parent.get("id"))
            parent_review_version = _required_positive_int(parent.get(self._mapping.parent_review_version))
            records = self._billing_records(parent_id)
            if not records:
                raise WorkbenchCandidateNotFoundError(SAFE_BILLING_NOT_FOUND)
            return tuple(
                _row(
                    record,
                    mapping=self._mapping,
                    parent_id=parent_id,
                    review_id=review_id,
                    company_id=company_id,
                    review_version=parent_review_version,
                )
                for record in _sorted_records(records, mapping=self._mapping)
            )
        except WorkbenchCandidateReadError:
            raise
        except ErpRepositoryError as exc:
            raise WorkbenchCandidateReadError(SAFE_BILLING_READ_ERROR) from exc

    def _parent_record(self, *, review_id: str, company_id: int) -> dict[str, Any]:
        records = self._adapter.search_read(
            model=self._mapping.parent_model,
            domain=[
                [self._mapping.parent_review_id, "=", review_id],
                [self._mapping.parent_company_id, "=", company_id],
            ],
            fields=[
                "id",
                self._mapping.parent_review_id,
                self._mapping.parent_company_id,
                self._mapping.parent_review_version,
            ],
            limit=2,
        )
        if not records:
            raise WorkbenchCandidateNotFoundError(SAFE_BILLING_NOT_FOUND)
        if len(records) > 1:
            raise WorkbenchCandidateAmbiguityError(SAFE_BILLING_AMBIGUITY_ERROR)
        company = _required_many2one_id(records[0].get(self._mapping.parent_company_id))
        returned_review_id = _required_text_value(records[0].get(self._mapping.parent_review_id))
        if company != company_id or returned_review_id != review_id:
            raise WorkbenchCandidateDataError(SAFE_BILLING_DATA_ERROR)
        return records[0]

    def _billing_records(self, parent_id: int) -> tuple[dict[str, Any], ...]:
        return self._adapter.search_read_all(
            model=self._mapping.billing_model,
            domain=[[self._mapping.parent_many2one_field, "=", parent_id]],
            fields=_unique_fields(
                "id",
                self._mapping.parent_many2one_field,
                self._mapping.billing_group_key,
                self._mapping.allocation_key,
                self._mapping.customer_id,
                self._mapping.product_id,
                self._mapping.description,
                self._mapping.quantity,
                self._mapping.unit_price,
                self._mapping.currency_id,
                self._mapping.sales_tax_ids,
                self._mapping.billing_ready,
                self._mapping.sequence,
            ),
        )


def _row(
    record: dict[str, Any],
    *,
    mapping: OdooWorkbenchBillingFieldMapping,
    parent_id: int,
    review_id: str,
    company_id: int,
    review_version: int,
) -> WorkbenchBillingAuthoringRow:
    try:
        linked_parent_id = _required_many2one_id(record.get(mapping.parent_many2one_field))
        if linked_parent_id != parent_id:
            raise WorkbenchCandidateDataError(SAFE_BILLING_DATA_ERROR)
        return WorkbenchBillingAuthoringRow(
            odoo_record_id=_required_many2one_id(record.get("id")),
            review_id=review_id,
            company_id=company_id,
            review_version=review_version,
            billing_group_key=_required_text_value(record.get(mapping.billing_group_key)),
            allocation_key=_required_text_value(record.get(mapping.allocation_key)),
            customer_id=_required_many2one_id(record.get(mapping.customer_id)),
            product_id=_required_many2one_id(record.get(mapping.product_id)),
            description=_required_text_value(record.get(mapping.description)),
            quantity=_required_positive_decimal(record.get(mapping.quantity)),
            unit_price=_required_positive_decimal(record.get(mapping.unit_price)),
            currency_id=_required_many2one_id(record.get(mapping.currency_id)),
            sales_tax_ids=_many2many_ids(record.get(mapping.sales_tax_ids)),
            billing_ready=_required_bool(record.get(mapping.billing_ready)),
            sequence=_optional_positive_int(record.get(mapping.sequence)) if mapping.sequence is not None else None,
        )
    except (InvalidOperation, TypeError, ValueError, WorkbenchContractError) as exc:
        raise WorkbenchCandidateDataError(SAFE_BILLING_DATA_ERROR) from exc


def _sorted_records(
    records: tuple[dict[str, Any], ...],
    *,
    mapping: OdooWorkbenchBillingFieldMapping,
) -> tuple[dict[str, Any], ...]:
    return tuple(
        sorted(
            records,
            key=lambda record: (
                _sort_text(record.get(mapping.billing_group_key)),
                _sort_sequence(record.get(mapping.sequence) if mapping.sequence is not None else None),
                _sort_text(record.get(mapping.allocation_key)),
                _required_many2one_id(record.get("id")),
            ),
        )
    )


def _required_many2one_id(value: Any) -> int:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, list | tuple) and value:
        first = value[0]
        if type(first) is int and first > 0:
            return first
    raise WorkbenchCandidateDataError(SAFE_BILLING_DATA_ERROR)


def _many2many_ids(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list | tuple) or not value:
        raise WorkbenchCandidateDataError(SAFE_BILLING_DATA_ERROR)
    ids: list[int] = []
    for item in value:
        if type(item) is int and item > 0:
            ids.append(item)
        elif isinstance(item, list | tuple) and item and type(item[0]) is int and item[0] > 0:
            ids.append(item[0])
        else:
            raise WorkbenchCandidateDataError(SAFE_BILLING_DATA_ERROR)
    if len(set(ids)) != len(ids):
        raise WorkbenchCandidateDataError(SAFE_BILLING_DATA_ERROR)
    return tuple(sorted(ids))


def _required_positive_decimal(value: Any) -> Decimal:
    if type(value) is bool:
        raise WorkbenchCandidateDataError(SAFE_BILLING_DATA_ERROR)
    if isinstance(value, float) and not math.isfinite(value):
        raise WorkbenchCandidateDataError(SAFE_BILLING_DATA_ERROR)
    if not isinstance(value, int | float | str | Decimal):
        raise WorkbenchCandidateDataError(SAFE_BILLING_DATA_ERROR)
    try:
        decimal = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise WorkbenchCandidateDataError(SAFE_BILLING_DATA_ERROR) from exc
    if not decimal.is_finite() or decimal <= Decimal("0"):
        raise WorkbenchCandidateDataError(SAFE_BILLING_DATA_ERROR)
    return decimal


def _required_positive_int(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise WorkbenchCandidateDataError(SAFE_BILLING_DATA_ERROR)
    return value


def _optional_positive_int(value: Any) -> int | None:
    if value is None or value is False:
        return None
    return _required_positive_int(value)


def _required_bool(value: Any) -> bool:
    if type(value) is bool:
        return value
    raise WorkbenchCandidateDataError(SAFE_BILLING_DATA_ERROR)


def _required_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(message)


def _required_text_value(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchCandidateDataError(SAFE_BILLING_DATA_ERROR)
    return value.strip()


def _sort_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _sort_sequence(value: Any) -> int:
    if type(value) is int and value > 0:
        return value
    return 0


def _unique_fields(*values: str | None) -> list[str]:
    seen: set[str] = set()
    fields: list[str] = []
    for value in values:
        if value is None or value in seen:
            continue
        seen.add(value)
        fields.append(value)
    return fields


def _require_mapping_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(message)


def _env(prefix: str, name: str) -> str:
    return os.environ.get(f"{prefix}{name}", "")


def _env_optional(prefix: str, name: str) -> str | None:
    value = os.environ.get(f"{prefix}{name}")
    if value is None or not value.strip():
        return None
    return value
