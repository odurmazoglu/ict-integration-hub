from __future__ import annotations

import asyncio
import dataclasses
import html
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any, Protocol

from app.application.execution import (
    ExecutionArtifact,
    ExecutionArtifactType,
    ExecutionMode,
    WorkbenchVendorBillExecutionResult,
    WorkbenchVendorBillExecutionStatus,
)
from app.application.workbench.dto import (
    ReviewDecisionAcknowledgement,
    ReviewDecisionType,
    ReviewReasonsRole,
    ReviewStatus,
)
from app.application.workbench.exceptions import (
    WorkbenchCandidateAmbiguityError,
    WorkbenchCandidateReadError,
    WorkbenchContractError,
    WorkbenchProjectionPublishError,
)
from app.application.workbench.projection import (
    ProjectionPublishResult,
    WorkbenchClassificationProjection,
    WorkbenchProjection,
    WorkbenchProjectionExecution,
    WorkbenchProjectionLineResolution,
)
from app.application.workbench.projection_sync_contracts import (
    ProductLineSyncResult,
    ProjectionFieldChange,
    ProjectionSyncOutcome,
    ProjectionSyncResult,
)
from app.application.workflow import WorkflowType
from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.exceptions import ErpRepositoryError, ErpRepositoryTimeoutError

SAFE_PROJECTION_READ_ERROR = "Odoo Workbench projection lookup failed."
SAFE_PROJECTION_WRITE_ERROR = "Odoo Workbench projection publish failed."
SAFE_PROJECTION_AMBIGUITY_ERROR = "Odoo Workbench projection lookup returned multiple records."
CURRENCY_MODEL = "res.currency"
#: Canonical ``ExecutionState.COMPLETED`` value (kept as text: the projection DTO is ERP/runtime neutral).
EXECUTION_STATE_COMPLETED = "completed"

ODOO_REVIEW_STATUS_BY_CANONICAL: dict[ReviewStatus, str] = {
    ReviewStatus.PENDING_REVIEW: "Pending Review",
    ReviewStatus.DECISION_SUBMITTED: "Decision Submitted",
    ReviewStatus.RESOLVED: "Resolved",
    ReviewStatus.DISMISSED: "Dismissed",
}

ODOO_WORKFLOW_BY_CANONICAL: dict[WorkflowType, str] = {
    WorkflowType.VENDOR_BILL: "Vendor Bill",
    WorkflowType.RFQ: "RFQ",
    WorkflowType.EXPENSE: "Expense",
    WorkflowType.ASSET: "Asset",
    WorkflowType.SUBSCRIPTION: "Subscription",
    WorkflowType.CUSTOMER_QUOTATION: "Customer Quotation",
    WorkflowType.MANUAL_REVIEW: "Manual Review",
}

ODOO_REVIEW_REQUIRED_BY_CANONICAL: dict[bool, str] = {
    True: "Yes",
    False: "No",
}

ODOO_BUSINESS_CONTEXT_REQUIRED_BY_CANONICAL: dict[bool, str] = {
    True: "Required",
    False: "Not Required",
}


def _projection_publish_error(exc: ErpRepositoryError) -> WorkbenchProjectionPublishError:
    safe_message = getattr(exc, "safe_message", None)
    if isinstance(safe_message, str) and safe_message.strip():
        return WorkbenchProjectionPublishError(f"{SAFE_PROJECTION_WRITE_ERROR} {safe_message}")
    return WorkbenchProjectionPublishError(SAFE_PROJECTION_WRITE_ERROR)


ODOO_EXECUTION_STATUS_BY_CANONICAL: dict[WorkbenchVendorBillExecutionStatus, str] = {
    WorkbenchVendorBillExecutionStatus.EXECUTED: "Executed",
    WorkbenchVendorBillExecutionStatus.ALREADY_EXECUTED: "Already Executed",
}


class WorkbenchProjectionClassificationService(Protocol):
    def get_projection(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> WorkbenchClassificationProjection:
        pass


class OdooWorkbenchProjectionAdapter(Protocol):
    def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int,
        offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        pass

    def create(self, *, model: str, values: dict[str, Any]) -> int:
        pass

    def write(self, *, model: str, record_id: int, values: dict[str, Any]) -> None:
        pass

    def read_selection_values(self, *, model: str, field_name: str) -> tuple[str, ...]:
        pass


class WorkbenchProductLineSyncPublisher(Protocol):
    """PR B child-row publisher (``OdooWorkbenchProductLinePublisher``)."""

    def sync_lines(
        self, projection: WorkbenchProjection, *, parent_record_id: int | None, apply: bool
    ) -> tuple[ProductLineSyncResult, ...]:
        pass


@dataclass(frozen=True, slots=True)
class OdooWorkbenchProjectionFieldMapping:
    model: str
    name: str
    review_id: str
    company_id: str
    invoice_number: str
    supplier: str
    supplier_tax_number: str
    invoice_date: str
    currency: str
    invoice_total: str
    review_status: str
    workflow: str
    review_version: str
    last_sync_at: str
    classification: str | None = None
    matched_rule: str | None = None
    rule_version: str | None = None
    review_required: str | None = None
    business_context_required: str | None = None
    conflict: str | None = None
    trace_id: str | None = None
    review_reasons: str | None = None
    warnings: str | None = None
    decision_ready: str | None = None
    decision_idempotency_key: str | None = None
    execution_status: str | None = None
    vendor_bill: str | None = None
    vendor_bill_external_identity: str | None = None
    execution_message: str | None = None
    #: OPS-UI-01A: Many2one ``res.currency`` field, resolved read-only from the ISO code.
    currency_id: str | None = None
    #: ADR-0013 operator guidance (all optional, Hub-owned): next-action selection, to-do
    #: HTML, completed-summary HTML, and the Many2many of eligible fixed-asset accounts.
    next_action: str | None = None
    todo: str | None = None
    completed: str | None = None
    eligible_asset_accounts: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "model",
            "name",
            "review_id",
            "company_id",
            "invoice_number",
            "supplier",
            "supplier_tax_number",
            "invoice_date",
            "currency",
            "invoice_total",
            "review_status",
            "workflow",
            "review_version",
            "last_sync_at",
        ):
            _require_mapping_text(getattr(self, field_name), f"{field_name} mapping is required.")
        _validate_optional_mapping_texts(self)

    @classmethod
    def from_environment(cls, *, prefix: str = "ODOO_WORKBENCH_PUBLISHER_") -> OdooWorkbenchProjectionFieldMapping:
        return cls(
            model=_env(prefix, "PARENT_MODEL"),
            name=_env(prefix, "NAME_FIELD"),
            review_id=_env(prefix, "REVIEW_ID_FIELD"),
            company_id=_env(prefix, "COMPANY_ID_FIELD"),
            invoice_number=_env(prefix, "INVOICE_NUMBER_FIELD"),
            supplier=_env(prefix, "SUPPLIER_FIELD"),
            supplier_tax_number=_env(prefix, "SUPPLIER_TAX_NUMBER_FIELD"),
            invoice_date=_env(prefix, "INVOICE_DATE_FIELD"),
            currency=_env(prefix, "CURRENCY_FIELD"),
            invoice_total=_env(prefix, "INVOICE_TOTAL_FIELD"),
            review_status=_env(prefix, "REVIEW_STATUS_FIELD"),
            workflow=_env(prefix, "WORKFLOW_FIELD"),
            review_version=_env(prefix, "REVIEW_VERSION_FIELD"),
            last_sync_at=_env(prefix, "LAST_SYNC_AT_FIELD"),
            classification=_env_optional(prefix, "CLASSIFICATION_FIELD"),
            matched_rule=_env_optional(prefix, "MATCHED_RULE_FIELD"),
            rule_version=_env_optional(prefix, "RULE_VERSION_FIELD"),
            review_required=_env_optional(prefix, "REVIEW_REQUIRED_FIELD"),
            business_context_required=_env_optional(prefix, "BUSINESS_CONTEXT_REQUIRED_FIELD"),
            conflict=_env_optional(prefix, "CONFLICT_FIELD"),
            trace_id=_env_optional(prefix, "TRACE_ID_FIELD"),
            review_reasons=_env_optional(prefix, "REVIEW_REASONS_FIELD"),
            warnings=_env_optional(prefix, "WARNINGS_FIELD"),
            decision_ready=_env_optional(prefix, "DECISION_READY_FIELD"),
            decision_idempotency_key=_env_optional(prefix, "DECISION_IDEMPOTENCY_KEY_FIELD"),
            execution_status=_env_optional(prefix, "EXECUTION_STATUS_FIELD"),
            vendor_bill=_env_optional(prefix, "VENDOR_BILL_FIELD"),
            vendor_bill_external_identity=_env_optional(prefix, "VENDOR_BILL_EXTERNAL_IDENTITY_FIELD"),
            execution_message=_env_optional(prefix, "EXECUTION_MESSAGE_FIELD"),
            currency_id=_env_optional(prefix, "CURRENCY_ID_FIELD"),
            next_action=_env_optional(prefix, "NEXT_ACTION_FIELD"),
            todo=_env_optional(prefix, "TODO_FIELD"),
            completed=_env_optional(prefix, "COMPLETED_FIELD"),
            eligible_asset_accounts=_env_optional(prefix, "ELIGIBLE_ASSET_ACCOUNTS_FIELD"),
        )


class OdooWorkbenchJson2ProjectionAdapter:
    """Narrow JSON-2 adapter for configured Odoo Studio Workbench projection rows."""

    def __init__(self, *, client: OdooJson2Client) -> None:
        self._client = client

    @classmethod
    def from_settings(cls, settings: Settings) -> OdooWorkbenchJson2ProjectionAdapter:
        return cls(client=OdooJson2Client.from_settings(settings))

    def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int,
        offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        try:
            return tuple(
                _run_sync(
                    self._client.search_read(
                        model=model,
                        domain=domain,
                        fields=fields,
                        limit=limit,
                        offset=offset,
                    )
                )
            )
        except ConnectorTimeoutError as exc:
            raise ErpRepositoryTimeoutError(exc.safe_message) from exc
        except ConnectorError as exc:
            raise ErpRepositoryError(exc.safe_message) from exc

    def create(self, *, model: str, values: dict[str, Any]) -> int:
        try:
            return _run_sync(self._client.create_studio_record(model=model, values=values))
        except ConnectorTimeoutError as exc:
            raise ErpRepositoryTimeoutError(exc.safe_message) from exc
        except ConnectorError as exc:
            raise ErpRepositoryError(exc.safe_message) from exc

    def write(self, *, model: str, record_id: int, values: dict[str, Any]) -> None:
        try:
            success = _run_sync(self._client.write_studio_record(model=model, record_id=record_id, values=values))
        except ConnectorTimeoutError as exc:
            raise ErpRepositoryTimeoutError(exc.safe_message) from exc
        except ConnectorError as exc:
            raise ErpRepositoryError(exc.safe_message) from exc
        if success is not True:
            raise ErpRepositoryError(SAFE_PROJECTION_WRITE_ERROR)

    def read_selection_values(self, *, model: str, field_name: str) -> tuple[str, ...]:
        try:
            return _run_sync(self._client.read_field_selection_values(model=model, field_name=field_name))
        except ConnectorTimeoutError as exc:
            raise ErpRepositoryTimeoutError(exc.safe_message) from exc
        except ConnectorError as exc:
            raise ErpRepositoryError(exc.safe_message) from exc


class OdooWorkbenchProjectionPublisher:
    """Publish Hub-owned Workbench projection fields to a configured Odoo Studio model."""

    def __init__(
        self,
        *,
        adapter: OdooWorkbenchProjectionAdapter,
        mapping: OdooWorkbenchProjectionFieldMapping,
        classification_service: WorkbenchProjectionClassificationService | None = None,
        product_line_publisher: WorkbenchProductLineSyncPublisher | None = None,
    ) -> None:
        self._adapter = adapter
        self._mapping = mapping
        self._classification_service = classification_service
        self._product_line_publisher = product_line_publisher
        self._selection_cache: dict[str, frozenset[str]] = {}
        self._currency_cache: dict[str, int] = {}

    def publish_projection(self, projection: WorkbenchProjection) -> ProjectionPublishResult:
        records = self._lookup(review_id=projection.review_id, company_id=projection.company_id)
        payload = self._projection_payload(projection)
        if not records:
            try:
                record_id = self._adapter.create(
                    model=self._mapping.model,
                    values={
                        self._mapping.name: projection.review_id,
                        self._mapping.review_id: projection.review_id,
                        self._mapping.company_id: projection.company_id,
                    }
                    | payload,
                )
            except ErpRepositoryError as exc:
                raise _projection_publish_error(exc) from exc
            return ProjectionPublishResult(
                review_id=projection.review_id,
                odoo_record_id=record_id,
                created=True,
                updated=False,
                version=projection.version,
            )

        record_id = _required_record_id(records[0])
        try:
            self._adapter.write(model=self._mapping.model, record_id=record_id, values=payload)
        except ErpRepositoryError as exc:
            raise _projection_publish_error(exc) from exc
        return ProjectionPublishResult(
            review_id=projection.review_id,
            odoo_record_id=record_id,
            created=False,
            updated=True,
            version=projection.version,
        )

    def republish_projection(self, projection: WorkbenchProjection) -> ProjectionPublishResult:
        """Update an already-created Workbench projection row; never create one.

        Unlike :meth:`publish_projection`, this method has no create branch. It is
        for re-projecting an existing review whose Odoo Workbench row was created at
        import time (e.g. after a supplier remediation + reclassification). A missing
        target row fails closed with :class:`WorkbenchProjectionPublishError`; two or
        more matches fail closed with :class:`WorkbenchCandidateAmbiguityError`. The
        target is resolved only from the trusted ``(review_id, company_id)`` lookup,
        exactly like :meth:`project_vendor_bill_execution_result`.
        """

        records = self._lookup(review_id=projection.review_id, company_id=projection.company_id)
        if not records:
            raise WorkbenchProjectionPublishError(SAFE_PROJECTION_WRITE_ERROR)
        record_id = _required_record_id(records[0])
        try:
            self._adapter.write(
                model=self._mapping.model,
                record_id=record_id,
                values=self._projection_payload(projection),
            )
        except ErpRepositoryError as exc:
            raise _projection_publish_error(exc) from exc
        return ProjectionPublishResult(
            review_id=projection.review_id,
            odoo_record_id=record_id,
            created=False,
            updated=True,
            version=projection.version,
        )

    def acknowledge_decision(
        self,
        acknowledgement: ReviewDecisionAcknowledgement,
        *,
        odoo_record_id: int,
        trace_id: str | None = None,
        idempotency_key: str | None = None,
        clear_ready: bool = False,
    ) -> ProjectionPublishResult:
        if type(odoo_record_id) is not int or odoo_record_id <= 0:
            raise WorkbenchContractError("odoo_record_id must be a positive ERP id.")
        if idempotency_key is not None and not idempotency_key.strip():
            raise WorkbenchContractError("idempotency_key must be non-empty when supplied.")
        if type(clear_ready) is not bool:
            raise WorkbenchContractError("clear_ready must be a boolean value.")
        values: dict[str, Any] = {
            self._mapping.review_status: _review_status_to_odoo(acknowledgement.status),
            self._mapping.review_version: acknowledgement.version,
        }
        _put_optional(values, self._mapping.trace_id, trace_id)
        if idempotency_key is not None:
            _put_optional(values, self._mapping.decision_idempotency_key, idempotency_key)
        if clear_ready:
            _put_optional(values, self._mapping.decision_ready, False)
        try:
            self._adapter.write(model=self._mapping.model, record_id=odoo_record_id, values=values)
        except ErpRepositoryError as exc:
            raise _projection_publish_error(exc) from exc
        return ProjectionPublishResult(
            review_id=acknowledgement.review_id,
            odoo_record_id=odoo_record_id,
            created=False,
            updated=True,
            version=acknowledgement.version,
            warnings=acknowledgement.warnings,
        )

    def project_vendor_bill_execution_result(
        self,
        result: WorkbenchVendorBillExecutionResult,
        *,
        trace_id: str | None = None,
    ) -> ProjectionPublishResult:
        _validate_execution_projection_result(result)
        records = self._lookup(review_id=result.review_id, company_id=result.company_id)
        if not records:
            raise WorkbenchProjectionPublishError(SAFE_PROJECTION_WRITE_ERROR)
        record_id = _required_record_id(records[0])
        payload = self._execution_result_payload(result, trace_id=trace_id)
        try:
            self._adapter.write(model=self._mapping.model, record_id=record_id, values=payload)
        except ErpRepositoryError as exc:
            raise _projection_publish_error(exc) from exc
        return ProjectionPublishResult(
            review_id=result.review_id,
            odoo_record_id=record_id,
            created=False,
            updated=True,
            version=result.decision_version,
            warnings=_execution_projection_warnings(self._mapping),
        )

    # ------------------------------------------------------------------ OPS-UI-01A full-snapshot sync

    def sync_projection(self, projection: WorkbenchProjection, *, apply: bool) -> ProjectionSyncResult:
        """Parent row first (unchanged OPS-UI-01A semantics), then the PR B product lines.

        Product lines are synchronized only when the projection carries them (child
        projection composed/enabled) and a line publisher is configured. They follow a
        parent that exists or would be created; a stale (skipped) parent skips its lines
        too. A product line failure is reported on the result and never changes the
        parent outcome.
        """

        result = self._sync_parent(projection, apply=apply)
        if self._product_line_publisher is None or result.outcome is ProjectionSyncOutcome.SKIPPED_STALE:
            return result
        if projection.product_line_error is not None:
            return dataclasses.replace(result, line_error=projection.product_line_error)
        if projection.product_lines is None:
            return result
        try:
            lines = self._product_line_publisher.sync_lines(
                projection, parent_record_id=result.odoo_record_id, apply=apply
            )
        except Exception as exc:  # noqa: BLE001 - a line failure must never turn a written parent into ERROR
            return dataclasses.replace(result, line_error=_line_error_message(exc))
        return dataclasses.replace(result, line_results=lines)

    def _sync_parent(self, projection: WorkbenchProjection, *, apply: bool) -> ProjectionSyncResult:
        """Diff one complete Hub snapshot against its Odoo row; write only a real change.

        * ``apply=False`` is a dry-run: lookups and read-only metadata/currency reads
          only, never a create or write.
        * Semantic equality ignores ``Last Sync At``; an equal row is never rewritten.
        * A snapshot older than the row (lower review version, or the same version
          with stored execution facts the snapshot would clear) is skipped, not written.
        * Unrepresentable values (currency, selection values) fail this review
          explicitly with :class:`WorkbenchProjectionPublishError`.
        """

        if type(apply) is not bool:
            raise WorkbenchContractError("apply must be a boolean value.")
        if projection.review_reasons_role is None:
            raise WorkbenchContractError("sync_projection requires a full-snapshot projection.")
        records = self._lookup(
            review_id=projection.review_id,
            company_id=projection.company_id,
            fields=self._sync_read_fields(),
        )
        desired = self._desired_values(projection)
        if not records:
            changes = _field_changes({}, desired, html_fields=self._html_fields(), m2m_fields=self._m2m_fields())
            if not apply:
                return _sync_result(projection, ProjectionSyncOutcome.CREATED, applied=False, changes=changes)
            values = {
                self._mapping.review_id: projection.review_id,
                self._mapping.company_id: projection.company_id,
            } | desired
            values[self._mapping.last_sync_at] = _datetime_text(datetime.now(UTC))
            values = self._m2m_write_values(values)
            try:
                record_id = self._adapter.create(model=self._mapping.model, values=values)
            except ErpRepositoryError as exc:
                raise _projection_publish_error(exc) from exc
            return _sync_result(
                projection, ProjectionSyncOutcome.CREATED, applied=True, changes=changes, odoo_record_id=record_id
            )

        existing = records[0]
        record_id = _required_record_id(existing)
        stale_reason = self._stale_reason(existing, projection, desired)
        if stale_reason is not None:
            return _sync_result(
                projection,
                ProjectionSyncOutcome.SKIPPED_STALE,
                applied=False,
                odoo_record_id=record_id,
                error=stale_reason,
            )
        changes = _field_changes(existing, desired, html_fields=self._html_fields(), m2m_fields=self._m2m_fields())
        if not changes:
            return _sync_result(projection, ProjectionSyncOutcome.NO_CHANGE, applied=False, odoo_record_id=record_id)
        if not apply:
            return _sync_result(
                projection, ProjectionSyncOutcome.UPDATED, applied=False, changes=changes, odoo_record_id=record_id
            )
        values = {change.field: desired[change.field] for change in changes}
        values[self._mapping.last_sync_at] = _datetime_text(datetime.now(UTC))
        values = self._m2m_write_values(values)
        try:
            self._adapter.write(model=self._mapping.model, record_id=record_id, values=values)
        except ErpRepositoryError as exc:
            raise _projection_publish_error(exc) from exc
        return _sync_result(
            projection, ProjectionSyncOutcome.UPDATED, applied=True, changes=changes, odoo_record_id=record_id
        )

    def _desired_values(self, projection: WorkbenchProjection) -> dict[str, Any]:
        """The complete Hub-owned field set for one row (never Last Sync At / trace / Odoo inputs)."""

        mapping = self._mapping
        values: dict[str, Any] = {
            mapping.name: projection.review_id,
            mapping.invoice_number: projection.invoice_number,
            mapping.supplier: projection.supplier_name,
            mapping.supplier_tax_number: projection.supplier_tax_number,
            mapping.invoice_date: _date_text(projection),
            mapping.currency: projection.currency,
            mapping.invoice_total: _decimal_value(projection.total_amount),
            mapping.review_status: _review_status_to_odoo(projection.status),
            mapping.workflow: _workflow_to_odoo(projection.workflow),
            mapping.review_version: projection.version,
        }
        if mapping.currency_id is not None:
            values[mapping.currency_id] = self._resolve_currency_id(projection.currency)
        _put_optional(values, mapping.review_reasons, _render_review_reasons(projection))
        _put_optional(values, mapping.warnings, _render_warning_badges(projection.warnings))
        classification = self._classification(projection)
        if classification is not None:
            values.update(self._classification_values(classification))
        values.update(self._execution_values(projection.execution))
        values.update(self._guidance_values(projection))
        self._require_representable_selections(values)
        return values

    def _guidance_values(self, projection: WorkbenchProjection) -> dict[str, Any]:
        guidance = projection.operator_guidance
        values: dict[str, Any] = {}
        if guidance is None:
            return values
        _put_optional(values, self._mapping.next_action, guidance.next_action.value)
        _put_optional(values, self._mapping.todo, guidance.todo_html)
        _put_optional(values, self._mapping.completed, guidance.completed_html)
        if self._mapping.eligible_asset_accounts is not None:
            values[self._mapping.eligible_asset_accounts] = list(guidance.eligible_asset_account_ids)
        return values

    def _execution_values(self, execution: WorkbenchProjectionExecution | None) -> dict[str, Any]:
        values: dict[str, Any] = {}
        completed = execution is not None and execution.state == EXECUTION_STATE_COMPLETED
        # Stored Hub state only: a completed execution is "Executed". "Already Executed"
        # describes a replayed *call*, not a state, so a pure snapshot never projects it.
        _put_optional(
            values,
            self._mapping.execution_status,
            ODOO_EXECUTION_STATUS_BY_CANONICAL[WorkbenchVendorBillExecutionStatus.EXECUTED] if completed else None,
        )
        _put_optional(values, self._mapping.vendor_bill, execution.vendor_bill_id if execution is not None else None)
        _put_optional(
            values,
            self._mapping.vendor_bill_external_identity,
            execution.vendor_bill_external_identity if execution is not None else None,
        )
        _put_optional(values, self._mapping.execution_message, _execution_message(execution))
        return values

    def _stale_reason(
        self, existing: dict[str, Any], projection: WorkbenchProjection, desired: dict[str, Any]
    ) -> str | None:
        existing_version = _normalized(existing.get(self._mapping.review_version))
        if isinstance(existing_version, int) and existing_version > projection.version:
            return (
                f"Odoo row is at review version {existing_version}, newer than the Hub snapshot "
                f"version {projection.version}; not overwritten."
            )
        if existing_version != projection.version:
            return None
        # Same review version: stored execution facts for one accepted decision only ever
        # advance, so a snapshot that would clear them is older than the row.
        for field_name in (self._mapping.vendor_bill, self._mapping.execution_status):
            if field_name is None:
                continue
            if _normalized(existing.get(field_name)) is not None and _normalized(desired.get(field_name)) is None:
                return (
                    f"Odoo row already shows stored execution facts ({field_name}) that this snapshot "
                    "does not contain; not overwritten."
                )
        return None

    def _resolve_currency_id(self, currency_code: str | None) -> int:
        code = currency_code.strip().upper() if isinstance(currency_code, str) else ""
        if not code:
            raise WorkbenchProjectionPublishError("Workbench projection currency code is missing.")
        cached = self._currency_cache.get(code)
        if cached is not None:
            return cached
        try:
            records = self._adapter.search_read(
                model=CURRENCY_MODEL,
                domain=[["name", "=", code], ["active", "in", [True, False]]],
                fields=["id", "name"],
                limit=2,
            )
        except ErpRepositoryError as exc:
            raise _projection_publish_error(exc) from exc
        if not records:
            raise WorkbenchProjectionPublishError(f"No Odoo currency matches invoice currency {code}.")
        if len(records) > 1:
            raise WorkbenchProjectionPublishError(f"Invoice currency {code} matches more than one Odoo currency.")
        record = records[0]
        currency_id = record.get("id")
        if type(currency_id) is not int or currency_id <= 0 or str(record.get("name", "")).strip().upper() != code:
            raise WorkbenchProjectionPublishError(f"Invoice currency {code} did not resolve to an exact Odoo currency.")
        self._currency_cache[code] = currency_id
        return currency_id

    def _require_representable_selections(self, values: dict[str, Any]) -> None:
        for field_name in (
            self._mapping.review_status,
            self._mapping.workflow,
            self._mapping.execution_status,
            self._mapping.review_required,
            self._mapping.business_context_required,
            self._mapping.next_action,
        ):
            if field_name is None or values.get(field_name) is None:
                continue
            allowed = self._selection_values(field_name)
            if values[field_name] not in allowed:
                raise WorkbenchProjectionPublishError(
                    f"Odoo field {field_name} has no selection value {values[field_name]!r}; "
                    "this review is not representable in the Workbench."
                )

    def _selection_values(self, field_name: str) -> frozenset[str]:
        cached = self._selection_cache.get(field_name)
        if cached is not None:
            return cached
        try:
            allowed = frozenset(self._adapter.read_selection_values(model=self._mapping.model, field_name=field_name))
        except ErpRepositoryError as exc:
            raise _projection_publish_error(exc) from exc
        self._selection_cache[field_name] = allowed
        return allowed

    def _sync_read_fields(self) -> list[str]:
        mapping = self._mapping
        names = [
            mapping.review_id,
            mapping.company_id,
            mapping.name,
            mapping.invoice_number,
            mapping.supplier,
            mapping.supplier_tax_number,
            mapping.invoice_date,
            mapping.currency,
            mapping.currency_id,
            mapping.invoice_total,
            mapping.review_status,
            mapping.workflow,
            mapping.review_version,
            mapping.review_reasons,
            mapping.warnings,
            mapping.classification,
            mapping.matched_rule,
            mapping.rule_version,
            mapping.review_required,
            mapping.business_context_required,
            mapping.conflict,
            mapping.execution_status,
            mapping.vendor_bill,
            mapping.vendor_bill_external_identity,
            mapping.execution_message,
            mapping.next_action,
            mapping.todo,
            mapping.completed,
            mapping.eligible_asset_accounts,
        ]
        return ["id", *dict.fromkeys(name for name in names if name is not None)]

    def _html_fields(self) -> frozenset[str]:
        return frozenset(
            name
            for name in (
                self._mapping.review_reasons,
                self._mapping.warnings,
                self._mapping.todo,
                self._mapping.completed,
            )
            if name is not None
        )

    def _m2m_fields(self) -> frozenset[str]:
        return frozenset({self._mapping.eligible_asset_accounts} - {None})

    def _m2m_write_values(self, values: dict[str, Any]) -> dict[str, Any]:
        """Many2many values are compared as id sets but written as one replace command."""

        return {
            name: ([[6, 0, sorted(value)]] if name in self._m2m_fields() else value) for name, value in values.items()
        }

    def _lookup(
        self, *, review_id: str, company_id: int, fields: list[str] | None = None
    ) -> tuple[dict[str, Any], ...]:
        try:
            records = self._adapter.search_read(
                model=self._mapping.model,
                domain=[
                    [self._mapping.review_id, "=", review_id],
                    [self._mapping.company_id, "=", company_id],
                ],
                fields=fields
                or ["id", self._mapping.review_id, self._mapping.company_id, self._mapping.review_version],
                limit=2,
            )
        except ErpRepositoryError as exc:
            raise WorkbenchCandidateReadError(SAFE_PROJECTION_READ_ERROR) from exc
        if len(records) > 1:
            raise WorkbenchCandidateAmbiguityError(SAFE_PROJECTION_AMBIGUITY_ERROR)
        return records

    def _projection_payload(self, projection: WorkbenchProjection) -> dict[str, Any]:
        classification = self._classification(projection)
        values: dict[str, Any] = {
            self._mapping.name: projection.review_id,
            self._mapping.invoice_number: projection.invoice_number,
            self._mapping.supplier: projection.supplier_name,
            self._mapping.supplier_tax_number: projection.supplier_tax_number,
            self._mapping.invoice_date: _date_text(projection),
            self._mapping.currency: projection.currency,
            self._mapping.invoice_total: _decimal_value(projection.total_amount),
            self._mapping.review_status: _review_status_to_odoo(projection.status),
            self._mapping.workflow: _workflow_to_odoo(projection.workflow),
            self._mapping.review_version: projection.version,
            self._mapping.last_sync_at: _datetime_text(projection.updated_at or datetime.now(UTC)),
        }
        _put_optional(values, self._mapping.trace_id, projection.trace_id)
        _put_optional(values, self._mapping.review_reasons, _render_reason_badges(projection.review_reasons))
        _put_optional(values, self._mapping.warnings, _render_warning_badges(projection.warnings))
        if classification is not None:
            values.update(self._classification_values(classification))
        return values

    def _classification_values(self, classification: WorkbenchClassificationProjection) -> dict[str, Any]:
        values: dict[str, Any] = {}
        _put_optional(
            values,
            self._mapping.classification,
            classification.classification_code or classification.status_label,
        )
        _put_optional(values, self._mapping.matched_rule, classification.matched_rule_name)
        _put_optional(values, self._mapping.rule_version, classification.matched_rule_version)
        _put_optional(
            values,
            self._mapping.review_required,
            ODOO_REVIEW_REQUIRED_BY_CANONICAL[classification.require_review],
        )
        _put_optional(
            values,
            self._mapping.business_context_required,
            ODOO_BUSINESS_CONTEXT_REQUIRED_BY_CANONICAL[classification.require_business_context],
        )
        _put_optional(
            values,
            self._mapping.conflict,
            classification.conflict_summary or classification.conflict_label,
        )
        return values

    def _classification(self, projection: WorkbenchProjection) -> WorkbenchClassificationProjection | None:
        if self._classification_service is None:
            return None
        return self._classification_service.get_projection(
            review_id=projection.review_id,
            company_id=projection.company_id,
            review_version=projection.classification_review_version or projection.version,
        )

    def _execution_result_payload(
        self,
        result: WorkbenchVendorBillExecutionResult,
        *,
        trace_id: str | None,
    ) -> dict[str, Any]:
        artifact = _single_vendor_bill_artifact(result)
        values: dict[str, Any] = {
            self._mapping.last_sync_at: _datetime_text(datetime.now(UTC)),
        }
        _put_optional(values, self._mapping.trace_id, trace_id)
        _put_optional(values, self._mapping.execution_status, ODOO_EXECUTION_STATUS_BY_CANONICAL[result.status])
        _put_optional(values, self._mapping.vendor_bill, int(artifact.artifact_id))
        _put_optional(values, self._mapping.vendor_bill_external_identity, artifact.external_identity)
        _put_optional(values, self._mapping.execution_message, result.message)
        return values


SAFE_PRODUCT_LINE_SYNC_ERROR = "Odoo Workbench product line sync failed."


def _line_error_message(exc: Exception) -> str:
    safe_message = getattr(exc, "safe_message", None)
    if isinstance(safe_message, str) and safe_message.strip():
        return safe_message.strip()
    if isinstance(exc, WorkbenchContractError):
        return str(exc)
    return SAFE_PRODUCT_LINE_SYNC_ERROR


def _review_status_to_odoo(status: ReviewStatus) -> str:
    try:
        return ODOO_REVIEW_STATUS_BY_CANONICAL[status]
    except KeyError as exc:
        raise WorkbenchContractError("Unsupported canonical review status for Odoo Workbench projection.") from exc


def _workflow_to_odoo(workflow: WorkflowType) -> str:
    try:
        return ODOO_WORKFLOW_BY_CANONICAL[workflow]
    except KeyError as exc:
        raise WorkbenchContractError("Unsupported canonical workflow for Odoo Workbench projection.") from exc


def _validate_execution_projection_result(result: WorkbenchVendorBillExecutionResult) -> None:
    if not isinstance(result, WorkbenchVendorBillExecutionResult):
        raise WorkbenchContractError("Workbench Vendor Bill execution result is required.")
    if result.mode is not ExecutionMode.EXECUTE:
        raise WorkbenchContractError("Only execute-mode Vendor Bill results may be projected as executed.")
    if result.status not in {
        WorkbenchVendorBillExecutionStatus.EXECUTED,
        WorkbenchVendorBillExecutionStatus.ALREADY_EXECUTED,
    }:
        raise WorkbenchContractError("Only successful Vendor Bill execution results may be projected.")


def _single_vendor_bill_artifact(result: WorkbenchVendorBillExecutionResult) -> ExecutionArtifact:
    artifacts = tuple(
        artifact for artifact in result.artifacts if artifact.artifact_type is ExecutionArtifactType.VENDOR_BILL
    )
    if len(artifacts) != 1:
        raise WorkbenchProjectionPublishError(SAFE_PROJECTION_WRITE_ERROR)
    return artifacts[0]


def _execution_projection_warnings(mapping: OdooWorkbenchProjectionFieldMapping) -> tuple[str, ...]:
    if any(
        getattr(mapping, field_name)
        for field_name in (
            "execution_status",
            "vendor_bill",
            "vendor_bill_external_identity",
            "execution_message",
        )
    ):
        return ()
    return ("No dedicated Odoo Workbench execution-result fields are configured.",)


def _render_reason_badges(reasons: tuple[object, ...]) -> str:
    return "".join(
        f'<span class="badge rounded-pill text-bg-warning">{html.escape(str(reason.message))}</span>'
        for reason in reasons
    )


def _render_warning_badges(warnings: tuple[str, ...]) -> str:
    return "".join(
        f'<span class="badge rounded-pill text-bg-danger">{html.escape(warning)}</span>' for warning in warnings
    )


# ---------------------------------------------------------------------- OPS-UI-01A rendering


def _render_review_reasons(projection: WorkbenchProjection) -> str:
    """Render reasons by their PR #197 lifecycle role.

    * ``current_blockers`` (pending): warning badges, exactly as before OPS-UI-01A.
    * ``decision_basis`` (decided/dismissed): neutral history under a heading, plus the
      effective resolution -- never presented as open errors.
    """

    if projection.review_reasons_role is not ReviewReasonsRole.DECISION_BASIS:
        return _render_reason_badges(projection.review_reasons)
    decision = projection.accepted_decision
    if decision is None:
        heading = "Decision basis"
    elif decision.decision_type is ReviewDecisionType.DISMISS:
        heading = f"Decision basis \u2014 dismissed (decision v{decision.decision_version})"
    else:
        heading = f"Decision basis \u2014 accepted decision v{decision.decision_version}"
    parts = [
        '<div class="o_ipp_decision_basis">',
        f'<div class="fw-bold">{html.escape(heading)}</div>',
        '<div class="text-muted">These findings required the accepted decision; they are not open blockers.</div>',
    ]
    parts.extend(
        f'<span class="badge rounded-pill text-bg-secondary">{html.escape(_reason_code(reason))}</span>'
        for reason in projection.review_reasons
    )
    parts.extend(
        f'<div class="o_ipp_effective_line">{html.escape(_resolution_text(resolution))}</div>'
        for resolution in projection.effective_resolutions
    )
    if projection.effective_state_error:
        parts.append(f'<div class="text-danger">{html.escape(projection.effective_state_error)}</div>')
    parts.append("</div>")
    return "".join(parts)


def _reason_code(reason: object) -> str:
    code = getattr(reason, "code", None)
    return str(getattr(code, "value", code) or getattr(reason, "message", ""))


#: Account-backed effective line kinds (``EffectiveLineResolutionKind`` values) and how
#: the Workbench names their source. Per-line account-only stays "account only";
#: OPS-UI-01A-1 adds the whole-invoice accounting-resolution / mapping sources.
_ACCOUNT_RESOLUTION_LABELS = {
    "account_only": "account only",
    "accounting_resolution": "accounting resolution",
    "operating_expense_mapping": "operating expense mapping",
}


def _resolution_text(resolution: WorkbenchProjectionLineResolution) -> str:
    line = f"Line {resolution.line_number}" if resolution.line_number else "Line"
    if resolution.kind == "product" and resolution.product_id is not None:
        source = (resolution.product_source or "").replace("_", " ")
        return f"{line} \u2192 product {resolution.product_id}" + (f" ({source})" if source else "")
    if resolution.kind == "fixed_asset" and resolution.asset_account_id is not None:
        return (
            f"{line} \u2192 asset account {resolution.asset_account_id}, "
            f"depreciation model {resolution.depreciation_model_id} (fixed asset)"
        )
    account_label = _ACCOUNT_RESOLUTION_LABELS.get(resolution.kind)
    if account_label is not None and resolution.expense_account_id is not None:
        return f"{line} \u2192 account {resolution.expense_account_id} ({account_label})"
    return f"{line} \u2192 unresolved"


def _execution_message(execution: WorkbenchProjectionExecution | None) -> str | None:
    """A factual description of *stored* Hub execution state -- never a live Odoo verification."""

    if execution is None:
        return None
    prefix = f"Stored Hub execution state: {execution.state} (decision v{execution.decision_version})."
    if execution.state == EXECUTION_STATE_COMPLETED:
        artifact = (
            f" Vendor Bill artifact: Odoo record {execution.vendor_bill_id}."
            if execution.vendor_bill_id is not None
            else " No Vendor Bill artifact is stored."
        )
        return prefix + artifact + " This is not a live Odoo readback."
    details = f" Retry count {execution.retry_count} of max attempts {execution.max_attempts}."
    if execution.vendor_bill_id is not None:
        details += f" Stored Vendor Bill artifact: Odoo record {execution.vendor_bill_id}."
    if execution.failure_message:
        details += f" Last failure: {execution.failure_message}"
    return prefix + details


# ---------------------------------------------------------------------- OPS-UI-01A semantic comparison


def _normalized(value: Any) -> Any:
    """Odoo read values vs. write values: False/""/0 are empty, Many2one reads are ids."""

    if value is None or value is False or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, list | tuple) and len(value) == 2 and type(value[0]) is int:
        return value[0]
    if isinstance(value, int | float):
        if value == 0:
            return None
        return round(float(value), 6) if isinstance(value, float) else value
    return value


class _HtmlTokens(HTMLParser):
    """Structure + text of stored HTML, ignoring Odoo's attribute-less wrappers/whitespace."""

    _WRAPPERS = frozenset({"span", "div", "p"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[tuple[Any, ...]] = []
        self._skipped: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        skip = tag in self._WRAPPERS and not attrs
        self._skipped.append(skip)
        if not skip:
            self.tokens.append(("start", tag, tuple(sorted((k, " ".join((v or "").split())) for k, v in attrs))))

    def handle_endtag(self, tag: str) -> None:
        skip = self._skipped.pop() if self._skipped else False
        if not skip:
            self.tokens.append(("end", tag))

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text:
            self.tokens.append(("text", text))


def _html_tokens(value: Any) -> tuple[tuple[Any, ...], ...] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parser = _HtmlTokens()
    parser.feed(value)
    parser.close()
    return tuple(parser.tokens) or None


def _html_text(value: Any) -> str | None:
    tokens = _html_tokens(value)
    if tokens is None:
        return None
    return " | ".join(token[1] for token in tokens if token[0] == "text")


def _field_changes(
    existing: dict[str, Any],
    desired: dict[str, Any],
    *,
    html_fields: frozenset[str],
    m2m_fields: frozenset[str] = frozenset(),
) -> tuple[ProjectionFieldChange, ...]:
    changes: list[ProjectionFieldChange] = []
    for field_name, after in desired.items():
        before = existing.get(field_name)
        if field_name in m2m_fields:
            before_ids, after_ids = _id_set(before), _id_set(after)
            if before_ids != after_ids:
                changes.append(ProjectionFieldChange(field=field_name, before=before_ids, after=after_ids))
            continue
        if field_name in html_fields:
            if _html_tokens(before) != _html_tokens(after):
                changes.append(
                    ProjectionFieldChange(field=field_name, before=_html_text(before), after=_html_text(after))
                )
            continue
        if _normalized(before) != _normalized(after):
            changes.append(
                ProjectionFieldChange(field=field_name, before=_normalized(before), after=_normalized(after))
            )
    return tuple(changes)


def _id_set(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(sorted({item for item in value if type(item) is int}))


def _sync_result(
    projection: WorkbenchProjection,
    outcome: ProjectionSyncOutcome,
    *,
    applied: bool,
    changes: tuple[ProjectionFieldChange, ...] = (),
    odoo_record_id: int | None = None,
    error: str | None = None,
) -> ProjectionSyncResult:
    return ProjectionSyncResult(
        review_id=projection.review_id,
        outcome=outcome,
        applied=applied,
        odoo_record_id=odoo_record_id,
        review_version=projection.version,
        changes=changes,
        error=error,
    )


def _date_text(projection: WorkbenchProjection) -> str | None:
    if projection.invoice_date is None:
        return None
    return projection.invoice_date.isoformat()


def _datetime_text(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _decimal_value(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _put_optional(values: dict[str, Any], field_name: str | None, value: Any) -> None:
    if field_name is None:
        return
    values[field_name] = value


def _required_record_id(record: dict[str, Any]) -> int:
    value = record.get("id")
    if type(value) is int and value > 0:
        return value
    raise WorkbenchCandidateReadError(SAFE_PROJECTION_READ_ERROR)


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


def _run_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise ErpRepositoryError("ERP adapter cannot run a synchronous request inside an active event loop.")
