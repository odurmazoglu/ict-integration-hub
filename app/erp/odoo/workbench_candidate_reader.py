from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from app.application.workbench.allocations import (
    AllocationCompleteness,
    BusinessContextAllocation,
    BusinessContextAllocationSet,
    BusinessContextAllocationType,
)
from app.application.workbench.dto import LineResolution, ReviewDecisionType, TaxResolution
from app.application.workbench.exceptions import (
    WorkbenchCandidateAmbiguityError,
    WorkbenchCandidateDataError,
    WorkbenchCandidateNotFoundError,
    WorkbenchCandidateReadError,
    WorkbenchContractError,
)
from app.application.workbench.projection import OdooWorkbenchDecisionCandidate
from app.application.workflow import WorkflowType
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.adapter import OdooReadOnlyAdapter

SAFE_CANDIDATE_READ_ERROR = "Odoo Workbench decision candidate read failed."
SAFE_CANDIDATE_DATA_ERROR = "Odoo Workbench decision candidate data is invalid."
SAFE_CANDIDATE_AMBIGUITY_ERROR = "Odoo Workbench decision candidate lookup returned multiple records."
SAFE_CANDIDATE_NOT_FOUND = "Odoo Workbench decision candidate was not found."


@dataclass(frozen=True, slots=True)
class OdooWorkbenchParentFieldMapping:
    model: str
    review_id: str
    company_id: str
    expected_version: str
    decision: str
    selected_workflow: str
    decision_ready: str
    decided_at: str
    decided_by: str
    idempotency_key: str
    allocation_one2many_field: str
    invoice_total: str
    currency: str
    selected_partner: str | None = None
    decision_comment: str | None = None
    line_resolutions: str | None = None
    tax_resolutions: str | None = None
    allocation_completeness: str | None = None
    fixed_allocation_completeness: AllocationCompleteness | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "model",
            "review_id",
            "company_id",
            "expected_version",
            "decision",
            "selected_workflow",
            "decision_ready",
            "decided_at",
            "decided_by",
            "idempotency_key",
            "allocation_one2many_field",
            "invoice_total",
            "currency",
        ):
            _require_mapping_text(getattr(self, field_name), f"{field_name} mapping is required.")
        _validate_optional_mapping_texts(self)
        if self.fixed_allocation_completeness is not None and not isinstance(
            self.fixed_allocation_completeness, AllocationCompleteness
        ):
            raise WorkbenchContractError("fixed_allocation_completeness must be canonical when supplied.")


@dataclass(frozen=True, slots=True)
class OdooWorkbenchAllocationFieldMapping:
    model: str
    parent_many2one_field: str
    allocation_key: str
    allocation_type: str
    amount: str
    percentage: str
    currency: str
    source_line_number: str | None = None
    description: str | None = None
    customer: str | None = None
    recharge_partner: str | None = None
    customer_invoice: str | None = None
    target_company: str | None = None
    opportunity: str | None = None
    sales_order: str | None = None
    sales_order_line: str | None = None
    proposal_scenario: str | None = None
    purchase_order: str | None = None
    project: str | None = None
    analytic_account: str | None = None
    subscription: str | None = None
    internal_note: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "model",
            "parent_many2one_field",
            "allocation_key",
            "allocation_type",
            "amount",
            "percentage",
            "currency",
        ):
            _require_mapping_text(getattr(self, field_name), f"{field_name} mapping is required.")
        _validate_optional_mapping_texts(self)


@dataclass(frozen=True, slots=True)
class OdooWorkbenchFieldMapping:
    parent: OdooWorkbenchParentFieldMapping
    allocation: OdooWorkbenchAllocationFieldMapping

    def __post_init__(self) -> None:
        if not isinstance(self.parent, OdooWorkbenchParentFieldMapping):
            raise WorkbenchContractError("parent mapping is required.")
        if not isinstance(self.allocation, OdooWorkbenchAllocationFieldMapping):
            raise WorkbenchContractError("allocation mapping is required.")

    @classmethod
    def from_environment(cls, *, prefix: str = "ODOO_WORKBENCH_") -> OdooWorkbenchFieldMapping:
        return cls(
            parent=OdooWorkbenchParentFieldMapping(
                model=_env(prefix, "PARENT_MODEL"),
                review_id=_env(prefix, "PARENT_REVIEW_ID_FIELD"),
                company_id=_env(prefix, "PARENT_COMPANY_ID_FIELD"),
                expected_version=_env(prefix, "PARENT_EXPECTED_VERSION_FIELD"),
                decision=_env(prefix, "PARENT_DECISION_FIELD"),
                selected_workflow=_env(prefix, "PARENT_SELECTED_WORKFLOW_FIELD"),
                decision_ready=_env(prefix, "PARENT_DECISION_READY_FIELD"),
                decided_at=_env(prefix, "PARENT_DECIDED_AT_FIELD"),
                decided_by=_env(prefix, "PARENT_DECIDED_BY_FIELD"),
                idempotency_key=_env(prefix, "PARENT_IDEMPOTENCY_KEY_FIELD"),
                allocation_one2many_field=_env(prefix, "PARENT_ALLOCATION_ONE2MANY_FIELD"),
                invoice_total=_env(prefix, "PARENT_INVOICE_TOTAL_FIELD"),
                currency=_env(prefix, "PARENT_CURRENCY_FIELD"),
                selected_partner=_env_optional(prefix, "PARENT_SELECTED_PARTNER_FIELD"),
                decision_comment=_env_optional(prefix, "PARENT_DECISION_COMMENT_FIELD"),
                line_resolutions=_env_optional(prefix, "PARENT_LINE_RESOLUTIONS_FIELD"),
                tax_resolutions=_env_optional(prefix, "PARENT_TAX_RESOLUTIONS_FIELD"),
                allocation_completeness=_env_optional(prefix, "PARENT_ALLOCATION_COMPLETENESS_FIELD"),
                fixed_allocation_completeness=_env_completeness(prefix, "FIXED_ALLOCATION_COMPLETENESS"),
            ),
            allocation=OdooWorkbenchAllocationFieldMapping(
                model=_env(prefix, "ALLOCATION_MODEL"),
                parent_many2one_field=_env(prefix, "ALLOCATION_PARENT_MANY2ONE_FIELD"),
                allocation_key=_env(prefix, "ALLOCATION_KEY_FIELD"),
                allocation_type=_env(prefix, "ALLOCATION_TYPE_FIELD"),
                amount=_env(prefix, "ALLOCATION_AMOUNT_FIELD"),
                percentage=_env(prefix, "ALLOCATION_PERCENTAGE_FIELD"),
                currency=_env(prefix, "ALLOCATION_CURRENCY_FIELD"),
                source_line_number=_env_optional(prefix, "ALLOCATION_SOURCE_LINE_NUMBER_FIELD"),
                description=_env_optional(prefix, "ALLOCATION_DESCRIPTION_FIELD"),
                customer=_env_optional(prefix, "ALLOCATION_CUSTOMER_FIELD"),
                recharge_partner=_env_optional(prefix, "ALLOCATION_RECHARGE_PARTNER_FIELD"),
                customer_invoice=_env_optional(prefix, "ALLOCATION_CUSTOMER_INVOICE_FIELD"),
                target_company=_env_optional(prefix, "ALLOCATION_TARGET_COMPANY_FIELD"),
                opportunity=_env_optional(prefix, "ALLOCATION_OPPORTUNITY_FIELD"),
                sales_order=_env_optional(prefix, "ALLOCATION_SALES_ORDER_FIELD"),
                sales_order_line=_env_optional(prefix, "ALLOCATION_SALES_ORDER_LINE_FIELD"),
                proposal_scenario=_env_optional(prefix, "ALLOCATION_PROPOSAL_SCENARIO_FIELD"),
                purchase_order=_env_optional(prefix, "ALLOCATION_PURCHASE_ORDER_FIELD"),
                project=_env_optional(prefix, "ALLOCATION_PROJECT_FIELD"),
                analytic_account=_env_optional(prefix, "ALLOCATION_ANALYTIC_ACCOUNT_FIELD"),
                subscription=_env_optional(prefix, "ALLOCATION_SUBSCRIPTION_FIELD"),
                internal_note=_env_optional(prefix, "ALLOCATION_INTERNAL_NOTE_FIELD"),
            ),
        )


class OdooWorkbenchDecisionCandidateReader:
    """Read-only adapter for Odoo Studio Workbench decision candidates."""

    def __init__(self, *, adapter: OdooReadOnlyAdapter, mapping: OdooWorkbenchFieldMapping) -> None:
        self._adapter = adapter
        self._mapping = mapping

    def list_ready_decisions(self, *, company_id: int, limit: int) -> tuple[OdooWorkbenchDecisionCandidate, ...]:
        if type(company_id) is not int or company_id <= 0:
            raise WorkbenchContractError("company_id must be positive.")
        if type(limit) is not int or limit <= 0:
            raise WorkbenchContractError("limit must be positive.")
        try:
            records = self._adapter.search_read(
                model=self._mapping.parent.model,
                domain=[
                    [self._mapping.parent.company_id, "=", company_id],
                    [self._mapping.parent.decision_ready, "=", True],
                ],
                fields=self._parent_fields(),
                limit=limit,
            )
            return tuple(
                candidate
                for record in records
                if (candidate := self._candidate_from_parent(record, requested_company_id=company_id)) is not None
            )
        except WorkbenchCandidateReadError:
            raise
        except ErpRepositoryError as exc:
            raise WorkbenchCandidateReadError(SAFE_CANDIDATE_READ_ERROR) from exc

    def get_ready_decision(self, *, review_id: str, company_id: int) -> OdooWorkbenchDecisionCandidate:
        _required_text(review_id, "review_id is required.")
        if type(company_id) is not int or company_id <= 0:
            raise WorkbenchContractError("company_id must be positive.")
        try:
            records = self._adapter.search_read(
                model=self._mapping.parent.model,
                domain=[
                    [self._mapping.parent.review_id, "=", review_id],
                    [self._mapping.parent.company_id, "=", company_id],
                ],
                fields=self._parent_fields(),
                limit=2,
            )
            if not records:
                raise WorkbenchCandidateNotFoundError(SAFE_CANDIDATE_NOT_FOUND)
            if len(records) > 1:
                raise WorkbenchCandidateAmbiguityError(SAFE_CANDIDATE_AMBIGUITY_ERROR)
            candidate = self._candidate_from_parent(records[0], requested_company_id=company_id)
            if candidate is None:
                raise WorkbenchCandidateNotFoundError(SAFE_CANDIDATE_NOT_FOUND)
            return candidate
        except WorkbenchCandidateReadError:
            raise
        except ErpRepositoryError as exc:
            raise WorkbenchCandidateReadError(SAFE_CANDIDATE_READ_ERROR) from exc

    def _candidate_from_parent(
        self,
        record: dict[str, Any],
        *,
        requested_company_id: int,
    ) -> OdooWorkbenchDecisionCandidate | None:
        try:
            ready = _required_bool(record.get(self._mapping.parent.decision_ready))
            if ready is False:
                return None
            parent_id = _required_many2one_id(record.get("id"))
            company_id = _required_many2one_id(record.get(self._mapping.parent.company_id))
            if company_id != requested_company_id:
                raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR)
            allocations = self._allocation_set(parent_record=record, parent_id=parent_id)
            return OdooWorkbenchDecisionCandidate(
                odoo_record_id=parent_id,
                review_id=_required_text_value(record.get(self._mapping.parent.review_id)),
                company_id=company_id,
                expected_version=_required_positive_int(record.get(self._mapping.parent.expected_version)),
                decision=ReviewDecisionType(_required_text_value(record.get(self._mapping.parent.decision))),
                idempotency_key=_required_text_value(record.get(self._mapping.parent.idempotency_key)),
                decided_by_odoo_user_id=_required_many2one_id(record.get(self._mapping.parent.decided_by)),
                decided_at=_required_aware_datetime(record.get(self._mapping.parent.decided_at)),
                decision_ready=True,
                selected_workflow=_optional_enum(
                    record.get(self._mapping.parent.selected_workflow),
                    WorkflowType,
                ),
                selected_partner_id=_optional_many2one_id(
                    _field(record, self._mapping.parent.selected_partner),
                ),
                line_resolutions=_line_resolutions(_field(record, self._mapping.parent.line_resolutions)),
                tax_resolutions=_tax_resolutions(_field(record, self._mapping.parent.tax_resolutions)),
                business_context_allocations=allocations,
                comment=_optional_text_value(_field(record, self._mapping.parent.decision_comment)),
            )
        except WorkbenchCandidateReadError:
            raise
        except (InvalidOperation, TypeError, ValueError, WorkbenchContractError) as exc:
            raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR) from exc

    def _allocation_set(
        self,
        *,
        parent_record: dict[str, Any],
        parent_id: int,
    ) -> BusinessContextAllocationSet | None:
        records = self._allocation_records(parent_id)
        if not records:
            return None
        completeness = self._allocation_completeness(parent_record)
        allocations = tuple(
            _allocation(record, self._mapping.allocation)
            for record in _sorted_allocations(records, key_field=self._mapping.allocation.allocation_key)
        )
        try:
            return BusinessContextAllocationSet(
                allocations=allocations,
                completeness=completeness,
                invoice_total=_optional_decimal(parent_record.get(self._mapping.parent.invoice_total)),
                currency=_optional_text_value(parent_record.get(self._mapping.parent.currency)),
            )
        except WorkbenchContractError as exc:
            raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR) from exc

    def _allocation_records(self, parent_id: int) -> tuple[dict[str, Any], ...]:
        try:
            return self._adapter.search_read_all(
                model=self._mapping.allocation.model,
                domain=[[self._mapping.allocation.parent_many2one_field, "=", parent_id]],
                fields=self._allocation_fields(),
            )
        except ErpRepositoryError as exc:
            raise WorkbenchCandidateReadError(SAFE_CANDIDATE_READ_ERROR) from exc

    def _allocation_completeness(self, parent_record: dict[str, Any]) -> AllocationCompleteness:
        if self._mapping.parent.allocation_completeness is not None:
            return AllocationCompleteness(
                _required_text_value(parent_record.get(self._mapping.parent.allocation_completeness))
            )
        if self._mapping.parent.fixed_allocation_completeness is not None:
            return self._mapping.parent.fixed_allocation_completeness
        raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR)

    def _parent_fields(self) -> list[str]:
        return _unique_fields(
            "id",
            self._mapping.parent.review_id,
            self._mapping.parent.company_id,
            self._mapping.parent.expected_version,
            self._mapping.parent.decision,
            self._mapping.parent.selected_workflow,
            self._mapping.parent.decision_ready,
            self._mapping.parent.decided_at,
            self._mapping.parent.decided_by,
            self._mapping.parent.idempotency_key,
            self._mapping.parent.allocation_one2many_field,
            self._mapping.parent.invoice_total,
            self._mapping.parent.currency,
            self._mapping.parent.selected_partner,
            self._mapping.parent.decision_comment,
            self._mapping.parent.line_resolutions,
            self._mapping.parent.tax_resolutions,
            self._mapping.parent.allocation_completeness,
        )

    def _allocation_fields(self) -> list[str]:
        return _unique_fields(
            "id",
            self._mapping.allocation.parent_many2one_field,
            self._mapping.allocation.allocation_key,
            self._mapping.allocation.allocation_type,
            self._mapping.allocation.amount,
            self._mapping.allocation.percentage,
            self._mapping.allocation.currency,
            self._mapping.allocation.source_line_number,
            self._mapping.allocation.description,
            self._mapping.allocation.customer,
            self._mapping.allocation.recharge_partner,
            self._mapping.allocation.customer_invoice,
            self._mapping.allocation.target_company,
            self._mapping.allocation.opportunity,
            self._mapping.allocation.sales_order,
            self._mapping.allocation.sales_order_line,
            self._mapping.allocation.proposal_scenario,
            self._mapping.allocation.purchase_order,
            self._mapping.allocation.project,
            self._mapping.allocation.analytic_account,
            self._mapping.allocation.subscription,
            self._mapping.allocation.internal_note,
        )


def _allocation(record: dict[str, Any], mapping: OdooWorkbenchAllocationFieldMapping) -> BusinessContextAllocation:
    return BusinessContextAllocation(
        allocation_key=_required_text_value(record.get(mapping.allocation_key)),
        allocation_type=BusinessContextAllocationType(_required_text_value(record.get(mapping.allocation_type))),
        source_line_number=_optional_text_value(_field(record, mapping.source_line_number)),
        description=_optional_text_value(_field(record, mapping.description)),
        amount=_optional_decimal(record.get(mapping.amount)),
        percentage=_optional_decimal(record.get(mapping.percentage)),
        currency=_optional_text_value(record.get(mapping.currency)),
        customer_id=_optional_many2one_id(_field(record, mapping.customer)),
        recharge_partner_id=_optional_many2one_id(_field(record, mapping.recharge_partner)),
        customer_invoice_id=_optional_many2one_id(_field(record, mapping.customer_invoice)),
        target_company_id=_optional_many2one_id(_field(record, mapping.target_company)),
        opportunity_id=_optional_many2one_id(_field(record, mapping.opportunity)),
        sales_order_id=_optional_many2one_id(_field(record, mapping.sales_order)),
        sales_order_line_id=_optional_many2one_id(_field(record, mapping.sales_order_line)),
        proposal_scenario_id=_optional_many2one_id(_field(record, mapping.proposal_scenario)),
        purchase_order_id=_optional_many2one_id(_field(record, mapping.purchase_order)),
        project_id=_optional_many2one_id(_field(record, mapping.project)),
        analytic_account_id=_optional_many2one_id(_field(record, mapping.analytic_account)),
        subscription_id=_optional_many2one_id(_field(record, mapping.subscription)),
        internal_note=_optional_text_value(_field(record, mapping.internal_note)),
    )


def _sorted_allocations(records: tuple[dict[str, Any], ...], *, key_field: str) -> tuple[dict[str, Any], ...]:
    return tuple(sorted(records, key=lambda record: (_sort_key(record, key_field), _sort_id(record))))


def _sort_key(record: dict[str, Any], key_field: str) -> str:
    value = record.get(key_field)
    return value if isinstance(value, str) else ""


def _sort_id(record: dict[str, Any]) -> int:
    return _required_many2one_id(record.get("id"))


def _line_resolutions(value: Any) -> tuple[LineResolution, ...]:
    if _is_empty_optional(value):
        return ()
    return tuple(
        LineResolution(
            line_number=_required_text_value(item.get("line_number")),
            selected_product_id=_required_many2one_id(item.get("selected_product_id")),
        )
        for item in _json_list(value)
    )


def _tax_resolutions(value: Any) -> tuple[TaxResolution, ...]:
    if _is_empty_optional(value):
        return ()
    return tuple(
        TaxResolution(
            line_number=_required_text_value(item.get("line_number")),
            tax_index=_required_nonnegative_int(item.get("tax_index")),
            selected_tax_id=_required_many2one_id(item.get("selected_tax_id")),
        )
        for item in _json_list(value)
    )


def _json_list(value: Any) -> list[dict[str, Any]]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, list):
        raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR)
    for item in decoded:
        if not isinstance(item, dict):
            raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR)
    return decoded


def _field(record: dict[str, Any], field_name: str | None) -> Any:
    if field_name is None:
        return None
    return record.get(field_name)


def _optional_enum[T: Enum](value: Any, enum_type: type[T]) -> T | None:
    if _is_empty_optional(value):
        return None
    return enum_type(_required_text_value(value))


def _required_bool(value: Any) -> bool:
    if type(value) is bool:
        return value
    raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR)


def _required_many2one_id(value: Any) -> int:
    parsed = _optional_many2one_id(value)
    if parsed is None:
        raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR)
    return parsed


def _optional_many2one_id(value: Any) -> int | None:
    if _is_empty_optional(value):
        return None
    if type(value) is int:
        if value <= 0:
            raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR)
        return value
    if isinstance(value, list | tuple) and value:
        first = value[0]
        if type(first) is int and first > 0:
            return first
    raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR)


def _required_positive_int(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR)
    return value


def _required_nonnegative_int(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR)
    return value


def _required_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(message)


def _required_text_value(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR)
    return value.strip()


def _optional_text_value(value: Any) -> str | None:
    if _is_empty_optional(value):
        return None
    if not isinstance(value, str):
        raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR)
    return value


def _optional_decimal(value: Any) -> Decimal | None:
    if _is_empty_optional(value):
        return None
    if type(value) is bool:
        raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR)
    if isinstance(value, float) and not math.isfinite(value):
        raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR)
    if not isinstance(value, int | float | str | Decimal):
        raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR)
    try:
        decimal = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR) from exc
    if not decimal.is_finite():
        raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR)
    return decimal


def _required_aware_datetime(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR) from exc
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise WorkbenchCandidateDataError(SAFE_CANDIDATE_DATA_ERROR)
    return value


def _is_empty_optional(value: Any) -> bool:
    return value is None or value is False


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


def _validate_optional_mapping_texts(mapping: object) -> None:
    for field_name in getattr(mapping, "__dataclass_fields__", ()):
        value = getattr(mapping, field_name)
        if value is not None and isinstance(value, str) and not value.strip():
            raise WorkbenchContractError(f"{field_name} mapping must be non-empty when supplied.")


def _env(prefix: str, name: str) -> str:
    return os.environ.get(f"{prefix}{name}", "")


def _env_optional(prefix: str, name: str) -> str | None:
    value = os.environ.get(f"{prefix}{name}")
    if value is None or not value.strip():
        return None
    return value


def _env_completeness(prefix: str, name: str) -> AllocationCompleteness | None:
    value = _env_optional(prefix, name)
    if value is None:
        return None
    return AllocationCompleteness(value)
