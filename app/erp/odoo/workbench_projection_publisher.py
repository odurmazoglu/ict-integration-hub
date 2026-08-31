from __future__ import annotations

import asyncio
import html
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from app.application.execution import (
    ExecutionArtifact,
    ExecutionArtifactType,
    ExecutionMode,
    WorkbenchVendorBillExecutionResult,
    WorkbenchVendorBillExecutionStatus,
)
from app.application.workbench.dto import ReviewDecisionAcknowledgement, ReviewStatus
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
)
from app.application.workflow import WorkflowType
from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.exceptions import ErpRepositoryError, ErpRepositoryTimeoutError

SAFE_PROJECTION_READ_ERROR = "Odoo Workbench projection lookup failed."
SAFE_PROJECTION_WRITE_ERROR = "Odoo Workbench projection publish failed."
SAFE_PROJECTION_AMBIGUITY_ERROR = "Odoo Workbench projection lookup returned multiple records."

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


@dataclass(frozen=True, slots=True)
class OdooWorkbenchProjectionFieldMapping:
    model: str
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
    execution_id: str | None = None
    execution_mode: str | None = None
    execution_runtime_state: str | None = None
    vendor_bill_id: str | None = None
    vendor_bill_external_identity: str | None = None
    vendor_bill_created: str | None = None
    execution_message: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "model",
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
            execution_id=_env_optional(prefix, "EXECUTION_ID_FIELD"),
            execution_mode=_env_optional(prefix, "EXECUTION_MODE_FIELD"),
            execution_runtime_state=_env_optional(prefix, "EXECUTION_RUNTIME_STATE_FIELD"),
            vendor_bill_id=_env_optional(prefix, "VENDOR_BILL_ID_FIELD"),
            vendor_bill_external_identity=_env_optional(prefix, "VENDOR_BILL_EXTERNAL_IDENTITY_FIELD"),
            vendor_bill_created=_env_optional(prefix, "VENDOR_BILL_CREATED_FIELD"),
            execution_message=_env_optional(prefix, "EXECUTION_MESSAGE_FIELD"),
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


class OdooWorkbenchProjectionPublisher:
    """Publish Hub-owned Workbench projection fields to a configured Odoo Studio model."""

    def __init__(
        self,
        *,
        adapter: OdooWorkbenchProjectionAdapter,
        mapping: OdooWorkbenchProjectionFieldMapping,
        classification_service: WorkbenchProjectionClassificationService | None = None,
    ) -> None:
        self._adapter = adapter
        self._mapping = mapping
        self._classification_service = classification_service

    def publish_projection(self, projection: WorkbenchProjection) -> ProjectionPublishResult:
        records = self._lookup(review_id=projection.review_id, company_id=projection.company_id)
        payload = self._projection_payload(projection)
        if not records:
            try:
                record_id = self._adapter.create(
                    model=self._mapping.model,
                    values={
                        self._mapping.review_id: projection.review_id,
                        self._mapping.company_id: projection.company_id,
                    }
                    | payload,
                )
            except ErpRepositoryError as exc:
                raise WorkbenchProjectionPublishError(SAFE_PROJECTION_WRITE_ERROR) from exc
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
            raise WorkbenchProjectionPublishError(SAFE_PROJECTION_WRITE_ERROR) from exc
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
            raise WorkbenchProjectionPublishError(SAFE_PROJECTION_WRITE_ERROR) from exc
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
            raise WorkbenchProjectionPublishError(SAFE_PROJECTION_WRITE_ERROR) from exc
        return ProjectionPublishResult(
            review_id=result.review_id,
            odoo_record_id=record_id,
            created=False,
            updated=True,
            version=result.decision_version,
            warnings=_execution_projection_warnings(self._mapping),
        )

    def _lookup(self, *, review_id: str, company_id: int) -> tuple[dict[str, Any], ...]:
        try:
            records = self._adapter.search_read(
                model=self._mapping.model,
                domain=[
                    [self._mapping.review_id, "=", review_id],
                    [self._mapping.company_id, "=", company_id],
                ],
                fields=["id", self._mapping.review_id, self._mapping.company_id, self._mapping.review_version],
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
            review_version=projection.version,
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
        _put_optional(values, self._mapping.execution_id, result.execution_id)
        _put_optional(values, self._mapping.execution_mode, result.mode.value)
        _put_optional(
            values,
            self._mapping.execution_runtime_state,
            result.runtime_state.value if result.runtime_state is not None else None,
        )
        _put_optional(values, self._mapping.vendor_bill_id, artifact.artifact_id)
        _put_optional(values, self._mapping.vendor_bill_external_identity, artifact.external_identity)
        _put_optional(values, self._mapping.vendor_bill_created, artifact.created)
        _put_optional(values, self._mapping.execution_message, result.message)
        return values


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
            "execution_id",
            "execution_mode",
            "execution_runtime_state",
            "vendor_bill_id",
            "vendor_bill_external_identity",
            "vendor_bill_created",
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


def _date_text(projection: WorkbenchProjection) -> str | None:
    if projection.invoice_date is None:
        return None
    return projection.invoice_date.isoformat()


def _datetime_text(value: datetime) -> str:
    return value.isoformat()


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
