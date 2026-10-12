"""PR D: Odoo JSON-2 adapter for per-line NEW product requests (ADR-0013 Hub-pull).

An operator fills the "Yeni Ürün" inputs on one Workbench child product line row
(``x_ipp_wb_product_line``) -- name, kind, category, unit of measure, optional Internal
Reference -- and presses the row's "Yeni Ürün Oluştur"; the Studio server action only
snapshots the projected review version, the requesting user and a timestamp, and sets
the *create* ready flag. This adapter reads those rows and turns each into an
``OperatorRequest`` with action ``PRODUCT_LINE_CREATE``.

Separate request channel. The create request has its own ready flag, version, requester
and timestamp, so the PR C mapping reader (``x_studio_ipp_req_ready``) never sees it and,
while the create gate is off, a submitted create request stays completely inert. Only
the three Hub-owned result fields (result, message, processed time) are shared with the
PR C mapping request, so the row shows one "Sonuç" column.

Trust boundary: identical to PR C (see ``workbench_product_line_request_reader``). The
line identity is accepted only when the Hub-written identity fields agree with the
deterministic line key and the parent Workbench row; seller code, description, match
state and projected product are never read; the operator inputs are shape-checked here
and every business rule is re-validated by the use case.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from app.application.workbench.exceptions import (
    ProductRemediationContractError,
    WorkbenchCandidateReadError,
    WorkbenchContractError,
)
from app.application.workbench.operator_request_ingestion import (
    OperatorRequest,
    OperatorRequestAction,
    OperatorRequestOutcome,
    OperatorRequestReadFailure,
)
from app.application.workbench.product_line_creation import NewProductIntent, NewProductKind
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.workbench_operator_request_reader import (
    _Json2Adapter,
    _many2one_id,
    _mapped,
    _optional_datetime,
    _optional_text,
    _positive_int,
    acknowledge_same_request,
    request_result_values,
)
from app.erp.odoo.workbench_product_line_request_reader import (
    DUPLICATE_LINE_MESSAGE,
    NOT_CURRENT_MESSAGE,
    OdooProductLineRequestFieldMapping,
    _duplicated_keys,
    _many2one_or_none,
    _plain_text,
    _require_consistent_parent,
    _verified_line_number,
)

CREATE_ENV_PREFIX = "ODOO_WORKBENCH_PRODUCT_LINE_NEW_"
SAFE_CREATE_REQUEST_READ_ERROR = "Odoo Workbench new product requests could not be read."

MISSING_CREATE_SUBMIT_MESSAGE = "İstek zamanı eksik; lütfen satırdaki 'Yeni Ürün Oluştur' düğmesini kullanın."
PENDING_MAPPING_MESSAGE = (
    "Bu satırda bekleyen bir 'Eşleştir' isteği var; aynı satır için yeni ürün isteği işlenmedi. Önce eşleştirme "
    "isteğinin sonucunu bekleyin."
)

#: Studio selection value (= label) -> kind. Type and storability in one explicit choice.
ODOO_KIND_BY_LABEL: dict[str, NewProductKind] = {
    "Hizmet": NewProductKind.SERVICE,
    "Stoksuz Mal": NewProductKind.GOODS,
    "Stoklu Mal": NewProductKind.STOCKED_GOODS,
}


@dataclass(frozen=True, slots=True)
class OdooProductLineCreateRequestFieldMapping:
    """Studio contract of the new-product request fields on the child model (PR D).

    ``line_requests`` is the PR C contract: its projection mapping gives the Hub-owned
    identity fields, its parent mapping the consistency check, its ready/product fields
    the cross-channel conflict check, and its result/message/processed-at fields are the
    shared Hub-owned result fields.
    """

    line_requests: OdooProductLineRequestFieldMapping
    name: str = "x_studio_ipp_new_name"
    kind: str = "x_studio_ipp_new_kind"
    category: str = "x_studio_ipp_new_categ"
    uom: str = "x_studio_ipp_new_uom"
    internal_reference: str = "x_studio_ipp_new_ref"
    ready: str = "x_studio_ipp_new_ready"
    expected_version: str = "x_studio_ipp_new_version"
    requested_by: str = "x_studio_ipp_new_requested_by"
    requested_at: str = "x_studio_ipp_new_requested_at"

    def __post_init__(self) -> None:
        if not isinstance(self.line_requests, OdooProductLineRequestFieldMapping):
            raise WorkbenchContractError("New product request mapping needs the product line request contract.")
        for name in self.field_attributes():
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise WorkbenchContractError(f"New product request {name} mapping must be a non-empty field name.")
        own = self.own_fields()
        if len(set(own)) != len(own):
            raise WorkbenchContractError("New product request field mappings must be distinct.")
        lines = self.line_requests.lines
        taken = {getattr(lines, item.name) for item in fields(lines) if item.name != "model"}
        taken |= set(self.line_requests.request_fields())
        overlap = sorted(set(own) & taken)
        if overlap:
            # Own fields must never alias a projection field or a PR C mapping request field.
            raise WorkbenchContractError(f"New product request fields overlap existing fields: {overlap}.")

    @classmethod
    def field_attributes(cls) -> tuple[str, ...]:
        return (
            "name",
            "kind",
            "category",
            "uom",
            "internal_reference",
            "ready",
            "expected_version",
            "requested_by",
            "requested_at",
        )

    @classmethod
    def input_attributes(cls) -> tuple[str, ...]:
        """The operator inputs cleared after a completed creation."""

        return ("name", "kind", "category", "uom", "internal_reference")

    def own_fields(self) -> tuple[str, ...]:
        return tuple(getattr(self, name) for name in self.field_attributes())

    @classmethod
    def from_environment(
        cls, *, line_requests: OdooProductLineRequestFieldMapping, prefix: str = CREATE_ENV_PREFIX
    ) -> OdooProductLineCreateRequestFieldMapping:
        """Runbook defaults, each overridable by ``ODOO_WORKBENCH_PRODUCT_LINE_NEW_<NAME>_FIELD``."""

        overrides: dict[str, str] = {}
        for name in cls.field_attributes():
            env_name = f"{prefix}{name.upper()}_FIELD"
            value = os.environ.get(env_name)
            if value is None:
                continue
            if not value.strip():
                raise WorkbenchContractError(f"{env_name} must be non-empty when set.")
            overrides[name] = value.strip()
        return cls(line_requests=line_requests, **overrides)

    def read_fields(self) -> list[str]:
        """Identity + own request fields + the PR C ready flag; never seller code/description/state."""

        lines = self.line_requests.lines
        names = (
            lines.parent,
            lines.line_key,
            lines.is_current,
            lines.review_id,
            lines.company_id,
            lines.line_number,
            self.line_requests.ready,
            *self.own_fields(),
        )
        return ["id", *dict.fromkeys(names)]


class OdooProductLineCreateRequestReader:
    """Read-only: list ready new-product rows for one company and verify each identity."""

    def __init__(self, *, adapter: _Json2Adapter, mapping: OdooProductLineCreateRequestFieldMapping) -> None:
        self._adapter = adapter
        self._mapping = mapping

    def list_pending(self, *, company_id: int, limit: int) -> tuple[OperatorRequest | OperatorRequestReadFailure, ...]:
        mapping = self._mapping
        lines = mapping.line_requests.lines
        try:
            records = self._adapter.search_read(
                model=lines.model,
                domain=[[lines.company_id, "=", company_id], [mapping.ready, "=", True]],
                fields=mapping.read_fields(),
                limit=limit,
            )
        except ErpRepositoryError as exc:
            raise WorkbenchCandidateReadError(SAFE_CREATE_REQUEST_READ_ERROR) from exc
        valid = sorted(
            (record for record in records if type(record.get("id")) is int and record["id"] > 0),
            key=lambda record: record["id"],
        )
        if not valid:
            return ()
        parents = self._parents(valid)
        duplicated = _duplicated_keys(valid, lines.line_key)
        return tuple(self._parse(record, company_id, parents, duplicated) for record in valid)

    def _parents(self, records: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        line_requests = self._mapping.line_requests
        ids = sorted(
            {
                parent_id
                for record in records
                if (parent_id := _many2one_or_none(record.get(line_requests.lines.parent))) is not None
            }
        )
        if not ids:
            return {}
        try:
            rows = self._adapter.search_read(
                model=line_requests.parent_model,
                domain=[["id", "in", ids]],
                fields=["id", line_requests.parent_review_id, line_requests.parent_company_id],
                limit=max(len(ids), 1),
            )
        except ErpRepositoryError as exc:
            raise WorkbenchCandidateReadError(SAFE_CREATE_REQUEST_READ_ERROR) from exc
        return {row["id"]: row for row in rows if type(row.get("id")) is int}

    def _parse(
        self,
        record: dict[str, Any],
        company_id: int,
        parents: dict[int, dict[str, Any]],
        duplicated: frozenset[str],
    ) -> OperatorRequest | OperatorRequestReadFailure:
        mapping = self._mapping
        lines = mapping.line_requests.lines
        record_id: int = record["id"]
        requested_at = _optional_datetime(record.get(mapping.requested_at))
        review_id = _plain_text(record.get(lines.review_id))
        try:
            if requested_at is None:
                raise WorkbenchContractError(MISSING_CREATE_SUBMIT_MESSAGE)
            if record.get(lines.is_current) is not True:
                raise WorkbenchContractError(NOT_CURRENT_MESSAGE)
            line_key = _plain_text(record.get(lines.line_key))
            if line_key is not None and line_key in duplicated:
                raise WorkbenchContractError(DUPLICATE_LINE_MESSAGE)
            if record.get(mapping.line_requests.ready) is True:
                raise WorkbenchContractError(PENDING_MAPPING_MESSAGE)
            line_number = _verified_line_number(record, lines, company_id, review_id, line_key)
            _require_consistent_parent(record, mapping.line_requests, parents, company_id, review_id)
            return OperatorRequest(
                odoo_record_id=record_id,
                review_id=review_id or "",
                company_id=company_id,
                action=OperatorRequestAction.PRODUCT_LINE_CREATE,
                expected_version=_positive_int(record.get(mapping.expected_version), "İnceleme sürümü"),
                requested_by_odoo_user_id=_many2one_id(record.get(mapping.requested_by), "İsteyen kullanıcı"),
                requested_at=requested_at,
                line_number=line_number,
                new_product=self._intent(record),
            )
        except (WorkbenchContractError, ProductRemediationContractError) as exc:
            return OperatorRequestReadFailure(
                odoo_record_id=record_id, review_id=review_id, requested_at=requested_at, message=_message(exc)
            )

    def _intent(self, record: dict[str, Any]) -> NewProductIntent:
        mapping = self._mapping
        name = _optional_text(record.get(mapping.name))
        if name is None:
            raise WorkbenchContractError("Yeni ürün adı girilmelidir.")
        return NewProductIntent(
            product_name=name,
            kind=_mapped(record.get(mapping.kind), ODOO_KIND_BY_LABEL, "Ürün türü"),
            categ_id=_many2one_id(record.get(mapping.category), "Ürün kategorisi"),
            uom_id=_many2one_id(record.get(mapping.uom), "Ölçü birimi"),
            internal_reference=_optional_text(record.get(mapping.internal_reference)),
        )


class OdooProductLineCreateRequestAcknowledger:
    """Write the Hub result to the child row; clear the create ready flag only for the same request."""

    def __init__(self, *, adapter: _Json2Adapter, mapping: OdooProductLineCreateRequestFieldMapping) -> None:
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
        shared = mapping.line_requests
        values = request_result_values(
            result_field=shared.result,
            message_field=shared.message,
            processed_at_field=shared.processed_at,
            ready_field=mapping.ready,
            outcome=outcome,
            message=message,
            processed_at=processed_at,
        )
        if clear_request_inputs:
            # A created product leaves nothing to resubmit; the projection now shows it as matched.
            for name in mapping.input_attributes():
                values[getattr(mapping, name)] = False
        return acknowledge_same_request(
            self._adapter,
            model=shared.lines.model,
            record_id=odoo_record_id,
            requested_at_field=mapping.requested_at,
            requested_at=requested_at,
            values=values,
        )


def _message(exc: Exception) -> str:
    message = getattr(exc, "safe_message", None)
    return message if isinstance(message, str) and message.strip() else str(exc)


__all__ = [
    "ODOO_KIND_BY_LABEL",
    "PENDING_MAPPING_MESSAGE",
    "OdooProductLineCreateRequestAcknowledger",
    "OdooProductLineCreateRequestFieldMapping",
    "OdooProductLineCreateRequestReader",
]
