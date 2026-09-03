from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.exceptions import ApplicationError
from app.application.quotation.contracts import QuotationScenarioLine, QuotationScenarioSnapshot
from app.application.quotation.evidence import QUOTATION_SCENARIO_EVIDENCE_SCHEMA_VERSION
from app.application.quotation.exceptions import (
    QuotationEvidenceConflictError,
    QuotationEvidenceDataIntegrityError,
    QuotationEvidenceError,
    QuotationEvidenceNotFoundError,
    QuotationEvidencePersistenceError,
)
from app.application.workbench.exceptions import WorkbenchContractError
from app.models.quotation_scenario_evidence import QuotationScenarioEvidence

SAFE_QUOTATION_EVIDENCE_ERROR = "Quotation scenario evidence could not be persisted safely."
SAFE_QUOTATION_EVIDENCE_NOT_FOUND = "Quotation scenario evidence was not found."
SAFE_QUOTATION_EVIDENCE_INTEGRITY_ERROR = "Quotation scenario evidence is invalid."
SAFE_QUOTATION_EVIDENCE_CONFLICT = "Quotation scenario evidence conflicts with stored evidence."


class SqlAlchemyQuotationScenarioEvidenceRepository:
    """Persist captured quotation scenario snapshots as immutable Hub evidence.

    First persistence inserts the immutable snapshot. An exact replay of an
    equivalent snapshot is idempotent and returns the stored evidence. A replay
    carrying the same semantic identity but a different commercial snapshot fails
    closed with :class:`QuotationEvidenceConflictError`. Stored evidence is never
    updated in place.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def persist(self, snapshot: QuotationScenarioSnapshot) -> QuotationScenarioSnapshot:
        _require_snapshot(snapshot)
        try:
            existing = self._find(snapshot)
            if existing is not None:
                return self._existing_or_conflict(existing, snapshot)
            record = _record_from_snapshot(snapshot)
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
            return _snapshot_from_record(record)
        except ApplicationError:
            raise
        except IntegrityError as exc:
            existing = self._find(snapshot)
            if existing is not None:
                return self._existing_or_conflict(existing, snapshot)
            raise QuotationEvidencePersistenceError(SAFE_QUOTATION_EVIDENCE_ERROR) from exc
        except SQLAlchemyError as exc:
            raise QuotationEvidencePersistenceError(SAFE_QUOTATION_EVIDENCE_ERROR) from exc

    def get(
        self,
        *,
        company_id: int,
        review_id: str,
        decision_id: str,
        decision_version: int,
        scenario_id: str,
    ) -> QuotationScenarioSnapshot:
        _require_positive_query_int(company_id)
        _require_query_text(review_id)
        _require_query_text(decision_id)
        _require_positive_query_int(decision_version)
        _require_query_text(scenario_id)
        try:
            record = self._session.scalar(
                select(QuotationScenarioEvidence).where(
                    QuotationScenarioEvidence.company_id == company_id,
                    QuotationScenarioEvidence.review_id == review_id,
                    QuotationScenarioEvidence.decision_id == decision_id,
                    QuotationScenarioEvidence.decision_version == decision_version,
                    QuotationScenarioEvidence.scenario_id == scenario_id,
                )
            )
        except SQLAlchemyError as exc:
            raise QuotationEvidencePersistenceError(SAFE_QUOTATION_EVIDENCE_ERROR) from exc
        if record is None:
            raise QuotationEvidenceNotFoundError(SAFE_QUOTATION_EVIDENCE_NOT_FOUND)
        return _snapshot_from_record(record)

    def _find(self, snapshot: QuotationScenarioSnapshot) -> QuotationScenarioEvidence | None:
        return self._session.scalar(
            select(QuotationScenarioEvidence).where(
                QuotationScenarioEvidence.company_id == snapshot.company_id,
                QuotationScenarioEvidence.review_id == snapshot.review_id,
                QuotationScenarioEvidence.decision_id == snapshot.decision_id,
                QuotationScenarioEvidence.decision_version == snapshot.decision_version,
                QuotationScenarioEvidence.scenario_id == snapshot.scenario_id,
            )
        )

    def _existing_or_conflict(
        self,
        record: QuotationScenarioEvidence,
        snapshot: QuotationScenarioSnapshot,
    ) -> QuotationScenarioSnapshot:
        stored = _snapshot_from_record(record)
        if _snapshot_fingerprint(stored) != _snapshot_fingerprint(snapshot):
            raise QuotationEvidenceConflictError(SAFE_QUOTATION_EVIDENCE_CONFLICT)
        return stored


def serialize_quotation_scenario_snapshot(snapshot: QuotationScenarioSnapshot) -> dict[str, Any]:
    """Return the canonical JSON payload for one captured quotation scenario.

    Monetary and quantity values are stored as canonical decimal strings and line
    order is preserved so the snapshot reconstructs exactly.
    """

    if not isinstance(snapshot, QuotationScenarioSnapshot):
        raise WorkbenchContractError("canonical QuotationScenarioSnapshot is required.")
    return {
        "schema_version": QUOTATION_SCENARIO_EVIDENCE_SCHEMA_VERSION,
        "scenario_id": snapshot.scenario_id,
        "scenario_name": snapshot.scenario_name,
        "company_id": snapshot.company_id,
        "customer_id": snapshot.customer_id,
        "opportunity_id": snapshot.opportunity_id,
        "currency": snapshot.currency,
        "review_id": snapshot.review_id,
        "decision_id": snapshot.decision_id,
        "decision_version": snapshot.decision_version,
        "selected": snapshot.selected,
        "lines": [
            {
                "line_id": line.line_id,
                "product_variant_id": line.product_variant_id,
                "quantity": _decimal_to_text(line.quantity),
                "sales_unit_price": _decimal_to_text(line.sales_unit_price),
                "cost_unit_price": _optional_decimal_to_text(line.cost_unit_price),
                "description": line.description,
                "uom_id": line.uom_id,
            }
            for line in snapshot.lines
        ],
    }


def deserialize_quotation_scenario_snapshot(data: dict[str, Any]) -> QuotationScenarioSnapshot:
    """Reconstruct the canonical quotation scenario snapshot from its JSON payload."""

    payload = _require_dict(data)
    if _required_int(payload.get("schema_version")) != QUOTATION_SCENARIO_EVIDENCE_SCHEMA_VERSION:
        raise QuotationEvidenceDataIntegrityError(SAFE_QUOTATION_EVIDENCE_INTEGRITY_ERROR)
    try:
        return QuotationScenarioSnapshot(
            scenario_id=_required_text(payload.get("scenario_id")),
            scenario_name=_required_text(payload.get("scenario_name")),
            company_id=_required_int(payload.get("company_id")),
            customer_id=_required_int(payload.get("customer_id")),
            opportunity_id=_optional_int(payload.get("opportunity_id")),
            currency=_required_text(payload.get("currency")),
            lines=tuple(_line_from_data(_require_dict(line)) for line in _require_list(payload.get("lines"))),
            review_id=_required_text(payload.get("review_id")),
            decision_id=_required_text(payload.get("decision_id")),
            decision_version=_required_int(payload.get("decision_version")),
            selected=_required_bool(payload.get("selected")),
        )
    except QuotationEvidenceError:
        raise
    except (WorkbenchContractError, InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise QuotationEvidenceDataIntegrityError(SAFE_QUOTATION_EVIDENCE_INTEGRITY_ERROR) from exc


def _line_from_data(data: dict[str, Any]) -> QuotationScenarioLine:
    return QuotationScenarioLine(
        line_id=_required_text(data.get("line_id")),
        product_variant_id=_required_int(data.get("product_variant_id")),
        quantity=_required_decimal(data.get("quantity")),
        sales_unit_price=_required_decimal(data.get("sales_unit_price")),
        cost_unit_price=_optional_decimal(data.get("cost_unit_price")),
        description=_optional_string(data.get("description")),
        uom_id=_optional_int(data.get("uom_id")),
    )


def _record_from_snapshot(snapshot: QuotationScenarioSnapshot) -> QuotationScenarioEvidence:
    return QuotationScenarioEvidence(
        company_id=snapshot.company_id,
        review_id=snapshot.review_id,
        decision_id=snapshot.decision_id,
        decision_version=snapshot.decision_version,
        scenario_id=snapshot.scenario_id,
        schema_version=QUOTATION_SCENARIO_EVIDENCE_SCHEMA_VERSION,
        scenario_snapshot=serialize_quotation_scenario_snapshot(snapshot),
    )


def _snapshot_from_record(record: QuotationScenarioEvidence) -> QuotationScenarioSnapshot:
    if record.schema_version != QUOTATION_SCENARIO_EVIDENCE_SCHEMA_VERSION:
        raise QuotationEvidenceDataIntegrityError(SAFE_QUOTATION_EVIDENCE_INTEGRITY_ERROR)
    snapshot = deserialize_quotation_scenario_snapshot(_require_dict(record.scenario_snapshot))
    identity = (
        record.company_id,
        record.review_id,
        record.decision_id,
        record.decision_version,
        record.scenario_id,
    )
    snapshot_identity = (
        snapshot.company_id,
        snapshot.review_id,
        snapshot.decision_id,
        snapshot.decision_version,
        snapshot.scenario_id,
    )
    if identity != snapshot_identity:
        raise QuotationEvidenceDataIntegrityError(SAFE_QUOTATION_EVIDENCE_INTEGRITY_ERROR)
    return snapshot


def _snapshot_fingerprint(snapshot: QuotationScenarioSnapshot) -> tuple[Any, ...]:
    return (
        snapshot.scenario_id,
        snapshot.scenario_name,
        snapshot.company_id,
        snapshot.customer_id,
        snapshot.opportunity_id,
        snapshot.currency,
        snapshot.review_id,
        snapshot.decision_id,
        snapshot.decision_version,
        snapshot.selected,
        tuple(
            (
                line.line_id,
                line.product_variant_id,
                _decimal_to_text(line.quantity),
                _decimal_to_text(line.sales_unit_price),
                _optional_decimal_to_text(line.cost_unit_price),
                line.description,
                line.uom_id,
            )
            for line in snapshot.lines
        ),
    )


def _require_snapshot(snapshot: QuotationScenarioSnapshot) -> None:
    if not isinstance(snapshot, QuotationScenarioSnapshot):
        raise WorkbenchContractError("canonical QuotationScenarioSnapshot is required.")


def _require_positive_query_int(value: Any) -> None:
    if type(value) is not int or value <= 0:
        raise QuotationEvidenceDataIntegrityError(SAFE_QUOTATION_EVIDENCE_INTEGRITY_ERROR)


def _require_query_text(value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise QuotationEvidenceDataIntegrityError(SAFE_QUOTATION_EVIDENCE_INTEGRITY_ERROR)


def _decimal_to_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise WorkbenchContractError("Decimal value is required.")
    return str(value)


def _optional_decimal_to_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return _decimal_to_text(value)


def _required_decimal(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise QuotationEvidenceDataIntegrityError(SAFE_QUOTATION_EVIDENCE_INTEGRITY_ERROR)
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise QuotationEvidenceDataIntegrityError(SAFE_QUOTATION_EVIDENCE_INTEGRITY_ERROR) from exc
    if not parsed.is_finite():
        raise QuotationEvidenceDataIntegrityError(SAFE_QUOTATION_EVIDENCE_INTEGRITY_ERROR)
    return parsed


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return _required_decimal(value)


def _required_int(value: Any) -> int:
    if type(value) is not int:
        raise QuotationEvidenceDataIntegrityError(SAFE_QUOTATION_EVIDENCE_INTEGRITY_ERROR)
    return value


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return _required_int(value)


def _required_bool(value: Any) -> bool:
    if type(value) is not bool:
        raise QuotationEvidenceDataIntegrityError(SAFE_QUOTATION_EVIDENCE_INTEGRITY_ERROR)
    return value


def _required_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuotationEvidenceDataIntegrityError(SAFE_QUOTATION_EVIDENCE_INTEGRITY_ERROR)
    return value


def _optional_string(value: Any) -> str | None:
    """Preserve any stored string (including empty) for optional free-text fields."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise QuotationEvidenceDataIntegrityError(SAFE_QUOTATION_EVIDENCE_INTEGRITY_ERROR)
    return value


def _require_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise QuotationEvidenceDataIntegrityError(SAFE_QUOTATION_EVIDENCE_INTEGRITY_ERROR)
    return value


def _require_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise QuotationEvidenceDataIntegrityError(SAFE_QUOTATION_EVIDENCE_INTEGRITY_ERROR)
    return value
