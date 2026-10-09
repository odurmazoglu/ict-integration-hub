"""PR C: Odoo JSON-2 adapter for per-line product mapping requests (ADR-0013 Hub-pull).

An operator selects an existing ``product.product`` on one Workbench child product line
row (``x_ipp_wb_product_line``) and presses the row's "Eşleştir" button; the Studio
server action only snapshots the projected review version, the requesting user and a
timestamp, and sets the ready flag. This adapter reads those ready rows and turns each
into an ``OperatorRequest`` with action ``PRODUCT_LINE_MAPPING`` for the existing
``OperatorRequestIngestionWorkflow`` -> ``ProductMappingRequestHandler`` ->
``MapExistingProductUseCase`` pipeline.

Trust boundary. The child row's request fields are operator input. Its projection fields
are written by the Hub, but Odoo ACLs are per model: the operator group needs write access
to edit the request fields, so a raw JSON-2 client could also write the Hub-owned ones.
Therefore nothing on the row is taken at face value:

* the line identity (company, review, source line) is accepted only when the four
  Hub-written identity fields agree with the deterministic Hub line key *and* with the
  Hub-written parent Workbench row the child hangs under; the row must still be current;
* seller code, description, supplier, match state and product projection fields are not
  even read -- the use case re-derives all of them from committed Hub evidence;
* the requester is authorized Hub-side exactly like a parent request (actor directory,
  existing permissions, narrow write authorization). As for the parent row (ADR-0013),
  ``requested_by`` is set by the Studio button and a raw JSON-2 writer could forge it, so
  the operator group's membership stays the Odoo-side trust boundary.

Any inconsistency fails closed for that row (``OperatorRequestReadFailure`` -> REJECTED),
and the acknowledgement writes only the Hub-owned result fields of the request.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from app.application.workbench.exceptions import WorkbenchCandidateReadError, WorkbenchContractError
from app.application.workbench.operator_request_ingestion import (
    OperatorRequest,
    OperatorRequestAction,
    OperatorRequestOutcome,
    OperatorRequestReadFailure,
)
from app.application.workbench.product_line_projection import product_line_key
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.workbench_operator_request_reader import (
    OdooOperatorRequestFieldMapping,
    _Json2Adapter,
    _many2one_id,
    _optional_datetime,
    _optional_many2one_id,
    _optional_text,
    _positive_int,
    acknowledge_same_request,
    request_result_values,
)
from app.erp.odoo.workbench_product_line_publisher import OdooWorkbenchProductLineFieldMapping

REQUEST_ENV_PREFIX = "ODOO_WORKBENCH_PRODUCT_LINE_REQ_"
SAFE_LINE_REQUEST_READ_ERROR = "Odoo Workbench product line requests could not be read."

MISSING_SUBMIT_MESSAGE = "İstek zamanı eksik; lütfen satırdaki 'Eşleştir' düğmesini kullanın."
NOT_CURRENT_MESSAGE = "Bu ürün satırı artık güncel değil; isteği güncel satırdan tekrar gönderin."
IDENTITY_MISMATCH_MESSAGE = "Ürün satırının kimliği Hub kaydıyla tutarsız; istek işlenmedi. Teknik destek alın."
PARENT_MISMATCH_MESSAGE = "Ürün satırı bağlı olduğu inceleme kaydıyla tutarsız; istek işlenmedi. Teknik destek alın."
DUPLICATE_LINE_MESSAGE = "Aynı fatura satırı için birden fazla bekleyen istek var; hiçbiri işlenmedi."


@dataclass(frozen=True, slots=True)
class OdooProductLineRequestFieldMapping:
    """Studio contract of the request fields on the child model (PR C).

    ``lines`` is the PR B projection contract (model + Hub-owned identity fields);
    ``parent_*`` are the parent Workbench request contract fields used for the parent
    consistency check. The request field defaults are the runbook's exact names.
    """

    lines: OdooWorkbenchProductLineFieldMapping
    parent_model: str
    parent_review_id: str
    parent_company_id: str
    product: str = "x_studio_ipp_req_product"
    ready: str = "x_studio_ipp_req_ready"
    expected_version: str = "x_studio_ipp_req_version"
    requested_by: str = "x_studio_ipp_req_requested_by"
    requested_at: str = "x_studio_ipp_req_requested_at"
    result: str = "x_studio_ipp_req_result"
    message: str = "x_studio_ipp_req_message"
    processed_at: str = "x_studio_ipp_req_processed_at"

    def __post_init__(self) -> None:
        if not isinstance(self.lines, OdooWorkbenchProductLineFieldMapping):
            raise WorkbenchContractError("Product line request mapping needs the product line projection contract.")
        for name in ("parent_model", "parent_review_id", "parent_company_id", *self.request_field_attributes()):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise WorkbenchContractError(f"Product line request {name} mapping must be a non-empty field name.")
        request_fields = self.request_fields()
        if len(set(request_fields)) != len(request_fields):
            raise WorkbenchContractError("Product line request field mappings must be distinct.")
        projection_fields = {getattr(self.lines, item.name) for item in fields(self.lines) if item.name != "model"}
        overlap = sorted(set(request_fields) & projection_fields)
        if overlap:
            # The acknowledgement writes request fields; it must never touch a Hub-owned projection field.
            raise WorkbenchContractError(f"Product line request fields overlap projection fields: {overlap}.")

    @classmethod
    def request_field_attributes(cls) -> tuple[str, ...]:
        return (
            "product",
            "ready",
            "expected_version",
            "requested_by",
            "requested_at",
            "result",
            "message",
            "processed_at",
        )

    def request_fields(self) -> tuple[str, ...]:
        return tuple(getattr(self, name) for name in self.request_field_attributes())

    @classmethod
    def from_environment(
        cls, *, parent: OdooOperatorRequestFieldMapping, prefix: str = REQUEST_ENV_PREFIX
    ) -> OdooProductLineRequestFieldMapping:
        """Runbook defaults, each overridable by ``ODOO_WORKBENCH_PRODUCT_LINE_REQ_<NAME>_FIELD``.

        A set-but-blank value is a contract error rather than a silent fallback.
        """

        overrides: dict[str, str] = {}
        for name in cls.request_field_attributes():
            env_name = f"{prefix}{name.upper()}_FIELD"
            value = os.environ.get(env_name)
            if value is None:
                continue
            if not value.strip():
                raise WorkbenchContractError(f"{env_name} must be non-empty when set.")
            overrides[name] = value.strip()
        return cls(
            lines=OdooWorkbenchProductLineFieldMapping.from_environment(),
            parent_model=parent.model,
            parent_review_id=parent.review_id,
            parent_company_id=parent.company_id,
            **overrides,
        )

    def read_fields(self) -> list[str]:
        """Identity + request fields only: seller code, description and match state are never read."""

        lines = self.lines
        names = (
            lines.parent,
            lines.line_key,
            lines.is_current,
            lines.review_id,
            lines.company_id,
            lines.line_number,
            self.product,
            self.ready,
            self.expected_version,
            self.requested_by,
            self.requested_at,
        )
        return ["id", *dict.fromkeys(names)]


class OdooProductLineRequestReader:
    """Read-only: list ready child line rows for one company and verify each identity."""

    def __init__(self, *, adapter: _Json2Adapter, mapping: OdooProductLineRequestFieldMapping) -> None:
        self._adapter = adapter
        self._mapping = mapping

    def list_pending(self, *, company_id: int, limit: int) -> tuple[OperatorRequest | OperatorRequestReadFailure, ...]:
        mapping = self._mapping
        try:
            records = self._adapter.search_read(
                model=mapping.lines.model,
                domain=[[mapping.lines.company_id, "=", company_id], [mapping.ready, "=", True]],
                fields=mapping.read_fields(),
                limit=limit,
            )
        except ErpRepositoryError as exc:
            raise WorkbenchCandidateReadError(SAFE_LINE_REQUEST_READ_ERROR) from exc
        valid = sorted(
            (record for record in records if type(record.get("id")) is int and record["id"] > 0),
            key=lambda record: record["id"],
        )
        if not valid:
            return ()
        parents = self._parents(valid)
        duplicated = _duplicated_keys(valid, mapping.lines.line_key)
        return tuple(self._parse(record, company_id, parents, duplicated) for record in valid)

    def _parents(self, records: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        mapping = self._mapping
        ids = sorted(
            {
                parent_id
                for record in records
                if (parent_id := _many2one_or_none(record.get(mapping.lines.parent))) is not None
            }
        )
        if not ids:
            return {}
        try:
            rows = self._adapter.search_read(
                model=mapping.parent_model,
                domain=[["id", "in", ids]],
                fields=["id", mapping.parent_review_id, mapping.parent_company_id],
                limit=max(len(ids), 1),
            )
        except ErpRepositoryError as exc:
            raise WorkbenchCandidateReadError(SAFE_LINE_REQUEST_READ_ERROR) from exc
        return {row["id"]: row for row in rows if type(row.get("id")) is int}

    def _parse(
        self,
        record: dict[str, Any],
        company_id: int,
        parents: dict[int, dict[str, Any]],
        duplicated: frozenset[str],
    ) -> OperatorRequest | OperatorRequestReadFailure:
        mapping = self._mapping
        lines = mapping.lines
        record_id: int = record["id"]
        requested_at = _optional_datetime(record.get(mapping.requested_at))
        review_id = _plain_text(record.get(lines.review_id))
        try:
            if requested_at is None:
                raise WorkbenchContractError(MISSING_SUBMIT_MESSAGE)
            if record.get(lines.is_current) is not True:
                raise WorkbenchContractError(NOT_CURRENT_MESSAGE)
            line_key = _plain_text(record.get(lines.line_key))
            if line_key is not None and line_key in duplicated:
                raise WorkbenchContractError(DUPLICATE_LINE_MESSAGE)
            line_number = _verified_line_number(record, lines, company_id, review_id, line_key)
            _require_consistent_parent(record, mapping, parents, company_id, review_id)
            return OperatorRequest(
                odoo_record_id=record_id,
                review_id=review_id or "",
                company_id=company_id,
                action=OperatorRequestAction.PRODUCT_LINE_MAPPING,
                expected_version=_positive_int(record.get(mapping.expected_version), "İnceleme sürümü"),
                requested_by_odoo_user_id=_many2one_id(record.get(mapping.requested_by), "İsteyen kullanıcı"),
                requested_at=requested_at,
                line_number=line_number,
                product_id=_optional_many2one_id(record.get(mapping.product), "Odoo ürünü"),
            )
        except WorkbenchContractError as exc:
            return OperatorRequestReadFailure(
                odoo_record_id=record_id, review_id=review_id, requested_at=requested_at, message=str(exc)
            )


class OdooProductLineRequestAcknowledger:
    """Write the Hub result to the child row; clear its ready flag only for the same request."""

    def __init__(self, *, adapter: _Json2Adapter, mapping: OdooProductLineRequestFieldMapping) -> None:
        self._adapter = adapter
        self._mapping = mapping

    def acknowledge(
        self,
        *,
        odoo_record_id: int,
        requested_at: datetime | None,
        outcome: OperatorRequestOutcome,
        message: str,
        processed_at: datetime,
        clear_request_inputs: bool = False,
    ) -> bool:
        mapping = self._mapping
        values = request_result_values(
            result_field=mapping.result,
            message_field=mapping.message,
            processed_at_field=mapping.processed_at,
            ready_field=mapping.ready,
            outcome=outcome,
            message=message,
            processed_at=processed_at,
        )
        if clear_request_inputs:
            # A completed mapping leaves nothing to resubmit; the projection shows the product.
            values[mapping.product] = False
        return acknowledge_same_request(
            self._adapter,
            model=mapping.lines.model,
            record_id=odoo_record_id,
            requested_at_field=mapping.requested_at,
            requested_at=requested_at,
            values=values,
        )


def _verified_line_number(
    record: dict[str, Any],
    lines: OdooWorkbenchProductLineFieldMapping,
    company_id: int,
    review_id: str | None,
    line_key: str | None,
) -> str:
    """The source line id, only when every Hub-written identity field agrees with the Hub key."""

    line_number = _plain_text(record.get(lines.line_number))
    row_company = record.get(lines.company_id)
    if review_id is None or line_number is None or line_key is None or row_company != company_id:
        raise WorkbenchContractError(IDENTITY_MISMATCH_MESSAGE)
    if product_line_key(company_id=company_id, review_id=review_id, line_number=line_number) != line_key:
        raise WorkbenchContractError(IDENTITY_MISMATCH_MESSAGE)
    return line_number


def _require_consistent_parent(
    record: dict[str, Any],
    mapping: OdooProductLineRequestFieldMapping,
    parents: dict[int, dict[str, Any]],
    company_id: int,
    review_id: str | None,
) -> None:
    parent_id = _many2one_or_none(record.get(mapping.lines.parent))
    parent = parents.get(parent_id) if parent_id is not None else None
    if parent is None:
        raise WorkbenchContractError(PARENT_MISMATCH_MESSAGE)
    if _plain_text(parent.get(mapping.parent_review_id)) != review_id:
        raise WorkbenchContractError(PARENT_MISMATCH_MESSAGE)
    if _many2one_or_none(parent.get(mapping.parent_company_id)) != company_id:
        raise WorkbenchContractError(PARENT_MISMATCH_MESSAGE)


def _duplicated_keys(records: list[dict[str, Any]], key_field: str) -> frozenset[str]:
    counts = Counter(key for record in records if (key := _plain_text(record.get(key_field))) is not None)
    return frozenset(key for key, count in counts.items() if count > 1)


def _many2one_or_none(value: Any) -> int | None:
    try:
        return _optional_many2one_id(value, "kayıt")
    except WorkbenchContractError:
        return None


def _plain_text(value: Any) -> str | None:
    try:
        return _optional_text(value)
    except WorkbenchContractError:
        return None


__all__ = [
    "DUPLICATE_LINE_MESSAGE",
    "IDENTITY_MISMATCH_MESSAGE",
    "NOT_CURRENT_MESSAGE",
    "PARENT_MISMATCH_MESSAGE",
    "OdooProductLineRequestAcknowledger",
    "OdooProductLineRequestFieldMapping",
    "OdooProductLineRequestReader",
]
