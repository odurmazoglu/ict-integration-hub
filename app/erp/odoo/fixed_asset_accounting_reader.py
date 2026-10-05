"""Read-only Odoo lookups behind CAPITALIZE_FIXED_ASSET validation.

Fixed models (``account.account``, ``account.depreciation.model``), fixed fields and an
id-only domain (archived records included so "inactive" is reported, not hidden). It
never creates, writes or posts anything -- it is built on :class:`OdooReadOnlyAdapter`.
Eligibility rules live in ``app.application.workbench.fixed_asset_lookup``.

``can_create_asset`` is verified via the sanctioned ``ir.model.fields`` metadata read
before it is requested; when this Odoo version has no such field the record carries
``None`` and the policy does not require it. The account-level asset posting accounts
(``asset_depreciation_account_id`` / ``asset_expense_account_id``, saas~19.2+) are
verified the same way and reported as unsupported when absent. The depreciation-model fields are also
verified first: a missing model or field fails closed instead of guessing.
"""

from __future__ import annotations

from typing import Any

from app.application.workbench.exceptions import FixedAssetAccountingUnavailableError
from app.application.workbench.fixed_asset_lookup import DepreciationModelRecord, FixedAssetAccountRecord
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.adapter import OdooReadOnlyAdapter

ACCOUNT_MODEL = "account.account"
DEPRECIATION_MODEL = "account.depreciation.model"
_ACCOUNT_FIELDS = ("id", "code", "name", "account_type", "active", "company_ids")
_ASSET_POSTING_FIELDS = ("asset_depreciation_account_id", "asset_expense_account_id")
_MODEL_FIELDS = ("id", "display_name", "active", "company_id", "method", "method_number", "method_period")
SAFE_FIXED_ASSET_LOOKUP_ERROR = "Odoo fixed-asset accounting lookup returned an unsafe response."


class OdooFixedAssetAccountingReader:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter
        self._can_create_asset_available: bool | None = None
        self._asset_posting_accounts_available: bool | None = None
        self._model_fields_validated = False

    def read_account(self, *, account_id: int) -> FixedAssetAccountRecord | None:
        _require_id(account_id)
        fields = list(_ACCOUNT_FIELDS)
        can_create = self._can_create_asset_supported()
        if can_create:
            fields.append("can_create_asset")
        posting = self._asset_posting_accounts_supported()
        if posting:
            fields.extend(_ASSET_POSTING_FIELDS)
        record = self._single(ACCOUNT_MODEL, account_id, fields)
        if record is None:
            return None
        try:
            return FixedAssetAccountRecord(
                id=_positive_int(record.get("id")),
                code=_text(record.get("code")),
                name=_text(record.get("name")),
                account_type=_text(record.get("account_type")),
                active=_bool(record.get("active")),
                company_ids=tuple(_positive_int(value) for value in _list(record.get("company_ids"))),
                can_create_asset=_bool(record.get("can_create_asset")) if can_create else None,
                asset_posting_accounts_supported=posting,
                asset_depreciation_account_id=_optional_many2one(record.get("asset_depreciation_account_id"))
                if posting
                else None,
                asset_expense_account_id=_optional_many2one(record.get("asset_expense_account_id"))
                if posting
                else None,
            )
        except (TypeError, ValueError) as exc:
            raise FixedAssetAccountingUnavailableError(SAFE_FIXED_ASSET_LOOKUP_ERROR) from exc

    def read_depreciation_model(self, *, model_id: int) -> DepreciationModelRecord | None:
        _require_id(model_id)
        self._ensure_model_fields()
        record = self._single(DEPRECIATION_MODEL, model_id, list(_MODEL_FIELDS))
        if record is None:
            return None
        try:
            company = record.get("company_id")
            return DepreciationModelRecord(
                id=_positive_int(record.get("id")),
                name=_text(record.get("display_name")),
                active=_bool(record.get("active")),
                company_id=_positive_int(company[0]) if isinstance(company, list | tuple) and company else None,
                method=record.get("method") or None,
                method_number=float(record["method_number"]) if record.get("method_number") is not None else None,
                method_period=str(record["method_period"]) if record.get("method_period") else None,
            )
        except (TypeError, ValueError) as exc:
            raise FixedAssetAccountingUnavailableError(SAFE_FIXED_ASSET_LOOKUP_ERROR) from exc

    def _single(self, model: str, record_id: int, fields: list[str]) -> dict[str, Any] | None:
        try:
            records = self._adapter.search_read(
                model=model,
                domain=[["id", "=", record_id], ["active", "in", [True, False]]],
                fields=fields,
                limit=2,
            )
        except ErpRepositoryError as exc:
            raise FixedAssetAccountingUnavailableError(SAFE_FIXED_ASSET_LOOKUP_ERROR) from exc
        if not records:
            return None
        if len(records) > 1 or not isinstance(records[0], dict):
            raise FixedAssetAccountingUnavailableError(SAFE_FIXED_ASSET_LOOKUP_ERROR)
        return records[0]

    def _can_create_asset_supported(self) -> bool:
        if self._can_create_asset_available is None:
            self._can_create_asset_available = self._field_exists(ACCOUNT_MODEL, "can_create_asset")
        return self._can_create_asset_available

    def _asset_posting_accounts_supported(self) -> bool:
        if self._asset_posting_accounts_available is None:
            self._asset_posting_accounts_available = all(
                self._field_exists(ACCOUNT_MODEL, name) for name in _ASSET_POSTING_FIELDS
            )
        return self._asset_posting_accounts_available

    def _ensure_model_fields(self) -> None:
        if self._model_fields_validated:
            return
        for name in ("active", "company_id"):
            if not self._field_exists(DEPRECIATION_MODEL, name):
                raise FixedAssetAccountingUnavailableError(
                    "This Odoo version exposes no usable account.depreciation.model."
                )
        self._model_fields_validated = True

    def _field_exists(self, model: str, field_name: str) -> bool:
        try:
            records = self._adapter.read_model_field_metadata(model=model, field_name=field_name)
        except ErpRepositoryError as exc:
            raise FixedAssetAccountingUnavailableError(SAFE_FIXED_ASSET_LOOKUP_ERROR) from exc
        return len(records) == 1 and records[0].get("name") == field_name


def _require_id(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise FixedAssetAccountingUnavailableError(SAFE_FIXED_ASSET_LOOKUP_ERROR)


def _positive_int(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("expected a positive integer")
    return value


def _optional_many2one(value: object) -> int | None:
    if value is False or value is None:
        return None
    if isinstance(value, list | tuple) and value:
        return _positive_int(value[0])
    return _positive_int(value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected text")
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("expected a boolean")
    return value


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("expected a list")
    return value


__all__ = ["OdooFixedAssetAccountingReader"]
