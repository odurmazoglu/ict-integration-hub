"""PR B: Hub -> Odoo per-line Workbench child projection (``x_ipp_wb_product_line``).

Read-only projection rows under one ``x_ipp_import_workbench`` parent row, upserted by
the Hub-owned line key. The Hub never reads anything *back* from these rows as
business input; Odoo is only the operator-facing view.

Upsert rules (one review at a time):

* rows are found by ``(review id, company id)`` -- every row, current or not -- and
  matched to projected lines by the Hub line key, never by Odoo record id,
  description or seller code;
* a missing row is created, a changed row gets only its changed fields written, an
  equal row is not touched (semantic comparison, Odoo read shapes normalized);
* currency is the explicit Hub-owned boolean ``x_studio_ipp_is_current`` (no native
  Odoo archive field is used or assumed): a row whose key is no longer projected is
  set to ``False`` (DEACTIVATE), never deleted; a row that becomes projected again is
  the same record set back to ``True``;
* two rows with the same key, or more rows than the safety cap, fail this review's
  lines closed before any write.

PR F / F3.1 advisory hint fields (optional, all-or-nothing): a Many2many of suggested
``product.product`` ids and a plain-text ranked summary. Unmapped (the default), the
publisher reads and writes exactly what it did before. Mapped, a line shows the hint the
projection carries only for a pending review's current PRODUCT_NOT_FOUND line; every other
line -- and every deactivated row -- gets both fields cleared. The Many2many is compared
as an id set and written as one replace command, exactly like the parent publisher's
eligible asset accounts. Operator input fields (``x_studio_ipp_req_*`` / ``x_studio_ipp_new_*``)
are never mapped or written here.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Any

from app.application.workbench.dto import ReviewStatus
from app.application.workbench.exceptions import WorkbenchContractError, WorkbenchProjectionPublishError
from app.application.workbench.product_line_projection import (
    ProductLineMatchState,
    WorkbenchProductLineProjection,
)
from app.application.workbench.projection import WorkbenchProjection
from app.application.workbench.projection_sync_contracts import (
    ProductLineSyncOutcome,
    ProductLineSyncResult,
)
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.workbench_projection_publisher import (
    OdooWorkbenchProjectionAdapter,
    _field_changes,
    _normalized,
)

ENV_PREFIX = "ODOO_WORKBENCH_PRODUCT_LINE_"
#: More child rows than this for one review is never a real invoice; fail closed.
MAX_LINES_PER_REVIEW = 500
SAFE_PRODUCT_LINE_READ_ERROR = "Odoo Workbench product line lookup failed."
SAFE_PRODUCT_LINE_WRITE_ERROR = "Odoo Workbench product line publish failed."
#: PR F / F3.1: optional advisory hint fields; mapped together or not at all.
SUGGESTION_FIELDS = ("suggested_products", "suggestion_summary")
#: Operator input prefixes owned by the PR C/D request readers; a projection never writes them.
OPERATOR_INPUT_PREFIXES = ("x_studio_ipp_req_", "x_studio_ipp_new_")


@dataclass(frozen=True, slots=True)
class OdooWorkbenchProductLineFieldMapping:
    """Studio contract of the child model; the defaults are the runbook's exact names."""

    model: str = "x_ipp_wb_product_line"
    parent: str = "x_studio_ipp_workbench_id"
    line_key: str = "x_studio_ipp_line_key"
    is_current: str = "x_studio_ipp_is_current"
    name: str = "x_name"
    review_id: str = "x_studio_ipp_review_id"
    company_id: str = "x_studio_ipp_company_id"
    review_version: str = "x_studio_ipp_review_version"
    line_number: str = "x_studio_ipp_line_number"
    line_sequence: str = "x_studio_ipp_line_sequence"
    supplier: str = "x_studio_ipp_supplier_id"
    seller_code: str = "x_studio_ipp_seller_code"
    description: str = "x_studio_ipp_description"
    quantity: str = "x_studio_ipp_quantity"
    uom_code: str = "x_studio_ipp_uom_code"
    match_state: str = "x_studio_ipp_match_state"
    product: str = "x_studio_ipp_product_id"
    matched_by: str = "x_studio_ipp_matched_by"
    message: str = "x_studio_ipp_line_message"
    #: PR F / F3.1 (optional, all-or-nothing): Many2many ``product.product`` + plain text.
    suggested_products: str | None = None
    suggestion_summary: str | None = None

    def __post_init__(self) -> None:
        names = [getattr(self, item.name) for item in fields(self)]
        for item, value in zip(fields(self), names, strict=True):
            if item.name in SUGGESTION_FIELDS and value is None:
                continue
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise WorkbenchContractError(f"Product line {item.name} mapping must be a non-empty field name.")
        if not self.model.startswith("x_"):
            raise WorkbenchContractError("Product line model must be an Odoo Studio (x_) model.")
        field_names = [name for name in names[1:] if name is not None]
        if len(set(field_names)) != len(field_names):
            raise WorkbenchContractError("Product line field mappings must be distinct.")
        hint_fields = [getattr(self, name) for name in SUGGESTION_FIELDS]
        if any(name is None for name in hint_fields) and any(name is not None for name in hint_fields):
            raise WorkbenchContractError(
                "Product line suggestion fields (SUGGESTED_PRODUCTS_FIELD, SUGGESTION_SUMMARY_FIELD) "
                "must be mapped together or not at all."
            )
        if any(name.startswith(OPERATOR_INPUT_PREFIXES) for name in field_names):
            raise WorkbenchContractError("Product line projection fields may not be operator input fields.")

    @property
    def suggestions_mapped(self) -> bool:
        return self.suggested_products is not None and self.suggestion_summary is not None

    @classmethod
    def from_environment(cls, *, prefix: str = ENV_PREFIX) -> OdooWorkbenchProductLineFieldMapping:
        """Runbook defaults, each overridable by ``ODOO_WORKBENCH_PRODUCT_LINE_<NAME>``.

        ``MODEL`` and ``<NAME>_FIELD`` (e.g. ``PARENT_FIELD``); a set-but-blank value
        is a contract error rather than a silent fallback to the default.
        """

        overrides: dict[str, str] = {}
        for item in fields(cls):
            env_name = f"{prefix}MODEL" if item.name == "model" else f"{prefix}{item.name.upper()}_FIELD"
            value = os.environ.get(env_name)
            if value is None:
                continue
            if not value.strip():
                raise WorkbenchContractError(f"{env_name} must be non-empty when set.")
            overrides[item.name] = value.strip()
        return cls(**overrides)

    def read_fields(self) -> list[str]:
        return [
            "id",
            *(getattr(self, item.name) for item in fields(self) if item.name != "model" and getattr(self, item.name)),
        ]

    def m2m_fields(self) -> frozenset[str]:
        return frozenset({self.suggested_products} - {None})


class OdooWorkbenchProductLinePublisher:
    """Diff/upsert the product line child rows of one Workbench parent row."""

    def __init__(
        self, *, adapter: OdooWorkbenchProjectionAdapter, mapping: OdooWorkbenchProductLineFieldMapping
    ) -> None:
        self._adapter = adapter
        self._mapping = mapping
        self._contract_verified = False

    def sync_lines(
        self,
        projection: WorkbenchProjection,
        *,
        parent_record_id: int | None,
        apply: bool,
    ) -> tuple[ProductLineSyncResult, ...]:
        """Plan (``apply=False``) or apply the child rows of ``projection``.

        ``parent_record_id`` is ``None`` only for a dry-run of a parent that does not
        exist yet; applying always requires the real parent row id.
        """

        lines = projection.product_lines
        if lines is None:
            raise WorkbenchContractError("sync_lines requires a composed product line projection.")
        if apply and (type(parent_record_id) is not int or parent_record_id <= 0):
            raise WorkbenchContractError("Applying product lines requires the parent Workbench record id.")
        self._verify_contract()
        existing = self._existing_rows(review_id=projection.review_id, company_id=projection.company_id)
        desired = {line.line_key: self._desired_values(projection, line, parent_record_id) for line in lines}
        if len(desired) != len(lines):
            raise WorkbenchProjectionPublishError("Product line keys are not unique for this review.")
        plan = self._plan(lines, desired, existing)
        if apply:
            self._apply(plan, desired)
        return tuple(_result(step) for step in plan)

    # ------------------------------------------------------------------ plan

    def _plan(
        self,
        lines: Iterable[WorkbenchProductLineProjection],
        desired: dict[str, dict[str, Any]],
        existing: dict[str | None, list[dict[str, Any]]],
    ) -> list[_Step]:
        plan: list[_Step] = []
        for line in lines:
            values = desired[line.line_key]
            rows = existing.get(line.line_key, [])
            if not rows:
                changes = _field_changes({}, values, html_fields=frozenset(), m2m_fields=self._mapping.m2m_fields())
                plan.append(_Step(line.line_key, line.line_number, ProductLineSyncOutcome.CREATED, None, changes))
                continue
            row = rows[0]
            changes = _field_changes(row, values, html_fields=frozenset(), m2m_fields=self._mapping.m2m_fields())
            outcome = ProductLineSyncOutcome.UPDATED if changes else ProductLineSyncOutcome.NO_CHANGE
            plan.append(_Step(line.line_key, line.line_number, outcome, _record_id(row), changes))
        for key, rows in existing.items():
            if key in desired:
                continue
            for row in rows:
                if _normalized(row.get(self._mapping.is_current)) is None:
                    continue  # already non-current: nothing to do
                plan.append(
                    _Step(
                        key or "",
                        _text(row.get(self._mapping.line_number)),
                        ProductLineSyncOutcome.DEACTIVATED,
                        _record_id(row),
                        _field_changes(
                            row,
                            self._deactivation_values(),
                            html_fields=frozenset(),
                            m2m_fields=self._mapping.m2m_fields(),
                        ),
                    )
                )
        return plan

    def _apply(self, plan: list[_Step], desired: dict[str, dict[str, Any]]) -> None:
        for step in plan:
            try:
                if step.outcome is ProductLineSyncOutcome.CREATED:
                    record_id = self._adapter.create(
                        model=self._mapping.model, values=self._write_values(desired[step.line_key])
                    )
                    step.record_id = record_id
                elif step.outcome is ProductLineSyncOutcome.UPDATED and step.record_id is not None:
                    values = {change.field: desired[step.line_key][change.field] for change in step.changes}
                    self._adapter.write(
                        model=self._mapping.model, record_id=step.record_id, values=self._write_values(values)
                    )
                elif step.outcome is ProductLineSyncOutcome.DEACTIVATED and step.record_id is not None:
                    deactivation = self._deactivation_values()
                    values = {
                        self._mapping.is_current: False,
                        **{change.field: deactivation[change.field] for change in step.changes},
                    }
                    self._adapter.write(
                        model=self._mapping.model, record_id=step.record_id, values=self._write_values(values)
                    )
            except ErpRepositoryError as exc:
                raise _publish_error(exc) from exc

    # ------------------------------------------------------------------ reads

    def _verify_contract(self) -> None:
        """The match-state selection must offer every Hub state before any line is written."""

        if self._contract_verified:
            return
        try:
            allowed = frozenset(
                self._adapter.read_selection_values(model=self._mapping.model, field_name=self._mapping.match_state)
            )
        except ErpRepositoryError as exc:
            raise WorkbenchProjectionPublishError(SAFE_PRODUCT_LINE_READ_ERROR) from exc
        missing = sorted(state.value for state in ProductLineMatchState if state.value not in allowed)
        if missing:
            raise WorkbenchProjectionPublishError(
                f"Odoo field {self._mapping.model}.{self._mapping.match_state} is missing selection keys "
                f"{', '.join(missing)}; the product line contract is not provisioned."
            )
        self._contract_verified = True

    def _existing_rows(self, *, review_id: str, company_id: int) -> dict[str | None, list[dict[str, Any]]]:
        mapping = self._mapping
        try:
            # Reading every mapped field also proves each one exists: Odoo rejects unknown fields.
            records = self._adapter.search_read(
                model=mapping.model,
                domain=[
                    [mapping.review_id, "=", review_id],
                    [mapping.company_id, "=", company_id],
                ],
                fields=mapping.read_fields(),
                limit=MAX_LINES_PER_REVIEW,
            )
        except ErpRepositoryError as exc:
            raise WorkbenchProjectionPublishError(
                f"{SAFE_PRODUCT_LINE_READ_ERROR} {getattr(exc, 'safe_message', '') or ''}".strip()
            ) from exc
        if len(records) >= MAX_LINES_PER_REVIEW:
            raise WorkbenchProjectionPublishError("Too many Odoo product line rows for one review; not synchronized.")
        grouped: dict[str | None, list[dict[str, Any]]] = {}
        for record in records:
            _record_id(record)
            grouped.setdefault(_text(record.get(mapping.line_key)), []).append(record)
        duplicated = sorted(key for key, rows in grouped.items() if key is not None and len(rows) > 1)
        if duplicated:
            raise WorkbenchProjectionPublishError(
                f"Duplicate Odoo product line rows for key {duplicated[0]}; not synchronized."
            )
        return grouped

    def _desired_values(
        self, projection: WorkbenchProjection, line: WorkbenchProductLineProjection, parent_record_id: int | None
    ) -> dict[str, Any]:
        mapping = self._mapping
        return {
            mapping.name: f"{projection.invoice_number or projection.review_id} · Satır {line.line_number}",
            mapping.parent: parent_record_id,
            mapping.line_key: line.line_key,
            mapping.is_current: True,
            mapping.review_id: line.review_id,
            mapping.company_id: line.company_id,
            mapping.review_version: line.review_version,
            mapping.line_number: line.line_number,
            mapping.line_sequence: line.line_sequence,
            mapping.supplier: line.supplier_partner_id,
            mapping.seller_code: line.seller_item_code,
            mapping.description: line.description,
            mapping.quantity: _quantity(line.quantity),
            mapping.uom_code: line.unit_code,
            mapping.match_state: line.match_state.value,
            mapping.product: line.product_id,
            mapping.matched_by: line.matched_by,
            mapping.message: line.message,
            **self._hint_values(projection, line),
        }

    # ------------------------------------------------------------------ PR F / F3.1 hints

    def _hint_values(self, projection: WorkbenchProjection, line: WorkbenchProductLineProjection) -> dict[str, Any]:
        """Hint fields only when mapped; a line outside the display contract is always cleared."""

        mapping = self._mapping
        if not mapping.suggestions_mapped:
            return {}
        hint = None
        if (
            projection.status is ReviewStatus.PENDING_REVIEW
            and line.match_state is ProductLineMatchState.PRODUCT_NOT_FOUND
            and line.review_version == projection.version
        ):
            hint = {item.line_key: item for item in projection.product_line_suggestions or ()}.get(line.line_key)
        return {
            mapping.suggested_products: list(hint.product_ids) if hint is not None else [],
            mapping.suggestion_summary: hint.summary if hint is not None else None,
        }

    def _deactivation_values(self) -> dict[str, Any]:
        mapping = self._mapping
        values: dict[str, Any] = {mapping.is_current: False}
        if mapping.suggestions_mapped:
            values[mapping.suggested_products] = []
            values[mapping.suggestion_summary] = None
        return values

    def _write_values(self, values: dict[str, Any]) -> dict[str, Any]:
        """Many2many values are compared as id sets but written as one replace command."""

        m2m = self._mapping.m2m_fields()
        return {name: ([[6, 0, sorted(value)]] if name in m2m else value) for name, value in values.items()}


@dataclass(slots=True)
class _Step:
    line_key: str
    line_number: str | None
    outcome: ProductLineSyncOutcome
    record_id: int | None
    changes: tuple[Any, ...]


def _result(step: _Step) -> ProductLineSyncResult:
    return ProductLineSyncResult(
        line_key=step.line_key,
        outcome=step.outcome,
        line_number=step.line_number,
        odoo_record_id=step.record_id,
        changes=step.changes,
    )


def _record_id(record: dict[str, Any]) -> int:
    value = record.get("id")
    if type(value) is int and value > 0:
        return value
    raise WorkbenchProjectionPublishError(SAFE_PRODUCT_LINE_READ_ERROR)


def _quantity(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _publish_error(exc: ErpRepositoryError) -> WorkbenchProjectionPublishError:
    safe_message = getattr(exc, "safe_message", None)
    if isinstance(safe_message, str) and safe_message.strip():
        return WorkbenchProjectionPublishError(f"{SAFE_PRODUCT_LINE_WRITE_ERROR} {safe_message}")
    return WorkbenchProjectionPublishError(SAFE_PRODUCT_LINE_WRITE_ERROR)


__all__ = [
    "MAX_LINES_PER_REVIEW",
    "SUGGESTION_FIELDS",
    "OdooWorkbenchProductLineFieldMapping",
    "OdooWorkbenchProductLinePublisher",
]
