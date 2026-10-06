"""Odoo JSON-2 adapter for Workbench operator requests (ADR-0013).

Reads ready request rows from the configured Workbench Studio model and writes the Hub
request result back. It touches only the request field group configured in
:class:`OdooOperatorRequestFieldMapping`; it never writes authoritative projection fields
(those belong to ``OdooWorkbenchProjectionPublisher``).

Studio selection keys are derived from labels, so both canonical values and the
documented Turkish labels are accepted when reading. Anything else fails closed for
that row.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any, Protocol

from app.application.workbench.accounting_resolution import AccountingTreatmentType
from app.application.workbench.exceptions import (
    WorkbenchCandidateReadError,
    WorkbenchContractError,
    WorkbenchProjectionPublishError,
)
from app.application.workbench.operator_request_ingestion import (
    OperatorRequest,
    OperatorRequestAction,
    OperatorRequestOutcome,
    OperatorRequestReadFailure,
)
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workbench.supplier_resolution import SupplierResolutionMode
from app.erp.exceptions import ErpRepositoryError

SAFE_REQUEST_READ_ERROR = "Odoo Workbench operator requests could not be read."
SAFE_REQUEST_ACK_ERROR = "Odoo Workbench operator request result could not be written."

ACTION_BY_ODOO_VALUE: dict[str, OperatorRequestAction] = {
    **{action.value: action for action in OperatorRequestAction},
    "Tedarikçi Çözümü": OperatorRequestAction.SUPPLIER_RESOLUTION,
    "Satın Alma Amacı": OperatorRequestAction.PURCHASE_PURPOSE,
    "Muhasebe İşlemi": OperatorRequestAction.ACCOUNTING_RESOLUTION,
    "Karar": OperatorRequestAction.DECISION,
    "Fatura Oluştur": OperatorRequestAction.EXECUTE_VENDOR_BILL,
}
SUPPLIER_MODE_BY_ODOO_VALUE: dict[str, SupplierResolutionMode] = {
    **{mode.value: mode for mode in SupplierResolutionMode},
    "Mevcut Tedarikçiyi Seç": SupplierResolutionMode.MATCH_EXISTING,
    "Kalıcı Tedarikçi Oluştur": SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER,
    "Tek Seferlik Tedarikçi": SupplierResolutionMode.ONE_OFF_VENDOR,
    "Tek Seferlik Niyet (Ertelenmiş)": SupplierResolutionMode.USE_ONE_OFF_SUPPLIER,
}
PURPOSE_BY_ODOO_VALUE: dict[str, PurchasePurpose] = {
    **{purpose.value: purpose for purpose in PurchasePurpose},
    "Şirket İçi Kullanım": PurchasePurpose.INTERNAL_USE,
    "Yeniden Satış": PurchasePurpose.RESALE,
    "Müşteri Projesi": PurchasePurpose.CUSTOMER_PROJECT,
    "Diğer İşletme Gideri": PurchasePurpose.OTHER_OPERATING_EXPENSE,
}
TREATMENT_BY_ODOO_VALUE: dict[str, AccountingTreatmentType] = {
    **{treatment.value: treatment for treatment in AccountingTreatmentType},
    "Gider Hesabı": AccountingTreatmentType.EXPENSE_ACCOUNT,
    "Sabit Kıymet / Demirbaş": AccountingTreatmentType.CAPITALIZE_FIXED_ASSET,
}
#: Result selection labels the Studio field must offer (documented in ODOO_WORKBENCH_OPERATOR_UI.md).
ODOO_RESULT_BY_OUTCOME: dict[OperatorRequestOutcome, str] = {
    OperatorRequestOutcome.COMPLETED: "Tamamlandı",
    OperatorRequestOutcome.ALREADY_COMPLETED: "Tamamlandı",
    OperatorRequestOutcome.STALE: "Güncel Değil",
    OperatorRequestOutcome.REJECTED: "Reddedildi",
    OperatorRequestOutcome.UNAUTHORIZED: "Yetkisiz",
    OperatorRequestOutcome.FAILED: "Hata",
}


class _Json2Adapter(Protocol):
    def search_read(
        self, *, model: str, domain: list[Any], fields: list[str], limit: int, offset: int = 0
    ) -> tuple[dict[str, Any], ...]: ...

    def write(self, *, model: str, record_id: int, values: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class OdooOperatorRequestFieldMapping:
    model: str
    review_id: str
    company_id: str
    ready: str
    action: str
    expected_version: str
    requested_by: str
    requested_at: str
    result: str
    message: str
    processed_at: str
    supplier_mode: str | None = None
    partner: str | None = None
    purchase_purpose: str | None = None
    treatment: str | None = None
    expense_account: str | None = None
    expense_category: str | None = None
    asset_account: str | None = None
    depreciation_model: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if item.default is not None and (not isinstance(value, str) or not value.strip()):
                raise WorkbenchContractError(f"{item.name} request mapping is required.")
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise WorkbenchContractError(f"{item.name} request mapping must be non-empty when supplied.")

    @classmethod
    def from_environment(cls, *, prefix: str = "ODOO_WORKBENCH_REQUEST_") -> OdooOperatorRequestFieldMapping:
        def required(name: str) -> str:
            value = os.getenv(f"{prefix}{name}", "").strip()
            if not value:
                raise WorkbenchContractError(f"{prefix}{name} is required for Workbench operator requests.")
            return value

        def optional(name: str) -> str | None:
            value = os.getenv(f"{prefix}{name}", "").strip()
            return value or None

        return cls(
            model=required("PARENT_MODEL"),
            review_id=required("REVIEW_ID_FIELD"),
            company_id=required("COMPANY_ID_FIELD"),
            ready=required("READY_FIELD"),
            action=required("ACTION_FIELD"),
            expected_version=required("EXPECTED_VERSION_FIELD"),
            requested_by=required("REQUESTED_BY_FIELD"),
            requested_at=required("REQUESTED_AT_FIELD"),
            result=required("RESULT_FIELD"),
            message=required("MESSAGE_FIELD"),
            processed_at=required("PROCESSED_AT_FIELD"),
            supplier_mode=optional("SUPPLIER_MODE_FIELD"),
            partner=optional("PARTNER_FIELD"),
            purchase_purpose=optional("PURCHASE_PURPOSE_FIELD"),
            treatment=optional("TREATMENT_FIELD"),
            expense_account=optional("EXPENSE_ACCOUNT_FIELD"),
            expense_category=optional("EXPENSE_CATEGORY_FIELD"),
            asset_account=optional("ASSET_ACCOUNT_FIELD"),
            depreciation_model=optional("DEPRECIATION_MODEL_FIELD"),
            note=optional("NOTE_FIELD"),
        )

    def read_fields(self) -> list[str]:
        names = [
            getattr(self, item.name)
            for item in fields(self)
            if item.name not in {"model", "result", "message", "processed_at"}
        ]
        return ["id", *dict.fromkeys(name for name in names if name is not None)]


class OdooOperatorRequestReader:
    """Read-only: list ready request rows for one company and parse them."""

    def __init__(self, *, adapter: _Json2Adapter, mapping: OdooOperatorRequestFieldMapping) -> None:
        self._adapter = adapter
        self._mapping = mapping

    def list_pending(self, *, company_id: int, limit: int) -> tuple[OperatorRequest | OperatorRequestReadFailure, ...]:
        try:
            records = self._adapter.search_read(
                model=self._mapping.model,
                domain=[[self._mapping.company_id, "=", company_id], [self._mapping.ready, "=", True]],
                fields=self._mapping.read_fields(),
                limit=limit,
            )
        except ErpRepositoryError as exc:
            raise WorkbenchCandidateReadError(SAFE_REQUEST_READ_ERROR) from exc
        # A row without a usable id cannot be acknowledged; JSON-2 always returns one.
        valid = [record for record in records if type(record.get("id")) is int and record["id"] > 0]
        return tuple(self._parse(record) for record in sorted(valid, key=lambda record: record["id"]))

    def _parse(self, record: dict[str, Any]) -> OperatorRequest | OperatorRequestReadFailure:
        mapping = self._mapping
        record_id: int = record["id"]
        requested_at = _optional_datetime(record.get(mapping.requested_at))
        review_id = _optional_text(record.get(mapping.review_id))
        try:
            if requested_at is None:
                raise WorkbenchContractError("İstek zamanı eksik; lütfen 'İşleme Gönder' düğmesini kullanın.")
            return OperatorRequest(
                odoo_record_id=record_id,
                review_id=review_id or "",
                company_id=_many2one_id(record.get(mapping.company_id), "Şirket"),
                action=_mapped(record.get(mapping.action), ACTION_BY_ODOO_VALUE, "İşlem türü"),
                expected_version=_positive_int(record.get(mapping.expected_version), "İnceleme sürümü"),
                requested_by_odoo_user_id=_many2one_id(record.get(mapping.requested_by), "İsteyen kullanıcı"),
                requested_at=requested_at,
                supplier_mode=_optional_mapped(
                    _get(record, mapping.supplier_mode), SUPPLIER_MODE_BY_ODOO_VALUE, "Tedarikçi işlemi"
                ),
                partner_id=_optional_many2one_id(_get(record, mapping.partner), "Tedarikçi"),
                purchase_purpose=_optional_mapped(
                    _get(record, mapping.purchase_purpose), PURPOSE_BY_ODOO_VALUE, "Satın alma amacı"
                ),
                treatment_type=_optional_mapped(
                    _get(record, mapping.treatment), TREATMENT_BY_ODOO_VALUE, "Muhasebe işlemi"
                ),
                expense_account_id=_optional_many2one_id(_get(record, mapping.expense_account), "Gider hesabı"),
                expense_category=_optional_text(_get(record, mapping.expense_category)),
                asset_account_id=_optional_many2one_id(_get(record, mapping.asset_account), "Demirbaş hesabı"),
                depreciation_model_id=_optional_many2one_id(
                    _get(record, mapping.depreciation_model), "Amortisman modeli"
                ),
                note=_optional_text(_get(record, mapping.note)),
            )
        except WorkbenchContractError as exc:
            return OperatorRequestReadFailure(
                odoo_record_id=record_id, review_id=review_id, requested_at=requested_at, message=str(exc)
            )


class OdooOperatorRequestAcknowledger:
    """Write the Hub result back; clear the ready flag only for the same request."""

    def __init__(self, *, adapter: _Json2Adapter, mapping: OdooOperatorRequestFieldMapping) -> None:
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
    ) -> bool:
        mapping = self._mapping
        try:
            rows = self._adapter.search_read(
                model=mapping.model,
                domain=[["id", "=", odoo_record_id]],
                fields=["id", mapping.ready, mapping.requested_at],
                limit=1,
            )
        except ErpRepositoryError as exc:
            raise WorkbenchProjectionPublishError(SAFE_REQUEST_ACK_ERROR) from exc
        if not rows:
            return False
        if _optional_datetime(rows[0].get(mapping.requested_at)) != requested_at:
            # The operator submitted a newer request meanwhile; never clear or overwrite it.
            return False
        values = {
            mapping.result: ODOO_RESULT_BY_OUTCOME[outcome],
            mapping.message: message,
            mapping.processed_at: processed_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            mapping.ready: False,
        }
        try:
            self._adapter.write(model=mapping.model, record_id=odoo_record_id, values=values)
        except ErpRepositoryError as exc:
            raise WorkbenchProjectionPublishError(SAFE_REQUEST_ACK_ERROR) from exc
        return True


def _get(record: dict[str, Any], field_name: str | None) -> Any:
    return record.get(field_name) if field_name is not None else None


def _empty(value: Any) -> bool:
    return value is None or value is False or value == "" or value == []


def _optional_text(value: Any) -> str | None:
    if _empty(value):
        return None
    if not isinstance(value, str):
        raise WorkbenchContractError("Metin alanı geçersiz.")
    text = value.strip()
    return text or None


def _mapped[T](value: Any, table: dict[str, T], label: str) -> T:
    text = _optional_text(value) if not _empty(value) else None
    if text is None:
        raise WorkbenchContractError(f"{label} seçilmelidir.")
    if text not in table:
        raise WorkbenchContractError(f"{label} değeri desteklenmiyor: {text}.")
    return table[text]


def _optional_mapped[T](value: Any, table: dict[str, T], label: str) -> T | None:
    if _empty(value):
        return None
    return _mapped(value, table, label)


def _many2one_id(value: Any, label: str) -> int:
    parsed = _optional_many2one_id(value, label)
    if parsed is None:
        raise WorkbenchContractError(f"{label} eksik.")
    return parsed


def _optional_many2one_id(value: Any, label: str) -> int | None:
    if _empty(value):
        return None
    candidate = value[0] if isinstance(value, list | tuple) and value else value
    if type(candidate) is not int or candidate <= 0:
        raise WorkbenchContractError(f"{label} değeri geçersiz.")
    return candidate


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(f"{label} geçersiz.")
    return value


def _optional_datetime(value: Any) -> datetime | None:
    if _empty(value) or not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    # Odoo stores and serves datetimes as naive UTC.
    return parsed.replace(tzinfo=UTC)


__all__ = [
    "ACTION_BY_ODOO_VALUE",
    "ODOO_RESULT_BY_OUTCOME",
    "PURPOSE_BY_ODOO_VALUE",
    "SUPPLIER_MODE_BY_ODOO_VALUE",
    "TREATMENT_BY_ODOO_VALUE",
    "OdooOperatorRequestAcknowledger",
    "OdooOperatorRequestFieldMapping",
    "OdooOperatorRequestReader",
]
