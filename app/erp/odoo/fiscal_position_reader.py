"""P0-PROD-18F-2: read-only fiscal-position evidence for RESALE execution account safety.

Reads exactly what :mod:`app.application.execution.resale_fiscal_position` needs to prove
a pinned RESALE account cannot be remapped -- the supplier's explicit fiscal position,
the company's automatically applicable fiscal positions, their account mappings -- and,
for Vendor Bill readback, the fiscal position Odoo put on a created bill. Every model,
domain and field list is fixed here, never caller-controlled.

Following the P0-PROD-18D/15P pattern, every field is verified via the sanctioned
``ir.model.fields`` metadata read (name, ``ttype`` and, for relational fields,
``relation``) before it is requested; any absent or structurally unexpected field fails
closed. Built on :class:`OdooReadOnlyAdapter`, which structurally cannot create, write,
unlink, post, pay, or reconcile anything.
"""

from __future__ import annotations

from typing import Any

from app.application.execution.resale_fiscal_position import (
    MAX_FISCAL_POSITION_ACCOUNT_MAPPINGS,
    MAX_FISCAL_POSITIONS,
    FiscalPositionAccountMappingRecord,
    FiscalPositionRecord,
    PartnerFiscalPositionRecord,
)
from app.erp.exceptions import ErpRepositoryResponseError
from app.erp.odoo.adapter import OdooReadOnlyAdapter

SAFE_FISCAL_POSITION_ERROR = "Odoo fiscal-position read returned an unsafe response."

COMPANY_MODEL = "res.company"
PARTNER_MODEL = "res.partner"
FISCAL_POSITION_MODEL = "account.fiscal.position"
FISCAL_POSITION_ACCOUNT_MODEL = "account.fiscal.position.account"
ACCOUNT_MODEL = "account.account"
MOVE_MODEL = "account.move"

MAX_VISIBLE_COMPANIES = 50

#: (field, ttype, relation) -- relation is ``None`` for non-relational fields.
_FieldSpec = tuple[str, str, str | None]

REQUIRED_FIELDS: dict[str, tuple[_FieldSpec, ...]] = {
    PARTNER_MODEL: (
        ("company_id", "many2one", COMPANY_MODEL),
        ("property_account_position_id", "many2one", FISCAL_POSITION_MODEL),
    ),
    FISCAL_POSITION_MODEL: (
        ("active", "boolean", None),
        ("company_id", "many2one", COMPANY_MODEL),
        ("auto_apply", "boolean", None),
        ("account_ids", "one2many", FISCAL_POSITION_ACCOUNT_MODEL),
    ),
    FISCAL_POSITION_ACCOUNT_MODEL: (
        ("position_id", "many2one", FISCAL_POSITION_MODEL),
        ("account_src_id", "many2one", ACCOUNT_MODEL),
        ("account_dest_id", "many2one", ACCOUNT_MODEL),
    ),
    MOVE_MODEL: (("fiscal_position_id", "many2one", FISCAL_POSITION_MODEL),),
}


class OdooFiscalPositionReader:
    """Structurally read-only reader behind ``FiscalPositionReader``."""

    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter
        self._validated_models: set[str] = set()

    def accessible_company_ids(self) -> tuple[int, ...]:
        records = self._adapter.search_read_all(
            model=COMPANY_MODEL, domain=[], fields=["id"], max_records=MAX_VISIBLE_COMPANIES + 1
        )
        if len(records) > MAX_VISIBLE_COMPANIES:
            raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR)
        ids = [_required_positive_int(_record(record).get("id")) for record in records]
        if len(set(ids)) != len(ids):
            raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR)
        return tuple(sorted(ids))

    def find_partner(self, *, company_id: int, partner_id: int) -> PartnerFiscalPositionRecord | None:
        _require_positive_argument(company_id)
        _require_positive_argument(partner_id)
        records = self._adapter.search_read(
            model=PARTNER_MODEL,
            domain=[
                ["id", "=", partner_id],
                ["company_id", "in", [company_id, False]],
                ["active", "in", [True, False]],
            ],
            fields=self._fields_for(PARTNER_MODEL),
            limit=2,
        )
        if not records:
            return None
        if len(records) > 1:
            raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR)
        record = _record(records[0])
        if _required_positive_int(record.get("id")) != partner_id:
            raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR)
        return _build(
            PartnerFiscalPositionRecord,
            partner_id=partner_id,
            company_id=_optional_many2one_id(record.get("company_id")),
            fiscal_position_id=_optional_many2one_id(record.get("property_account_position_id")),
        )

    def find_fiscal_position(self, *, fiscal_position_id: int) -> FiscalPositionRecord | None:
        _require_positive_argument(fiscal_position_id)
        # An explicit partner fiscal position applies even if archived, so archived ones are read too.
        records = self._adapter.search_read(
            model=FISCAL_POSITION_MODEL,
            domain=[["id", "=", fiscal_position_id], ["active", "in", [True, False]]],
            fields=self._fields_for(FISCAL_POSITION_MODEL),
            limit=2,
        )
        if not records:
            return None
        if len(records) > 1:
            raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR)
        position = self._fiscal_position(records[0])
        if position.id != fiscal_position_id:
            raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR)
        return position

    def list_auto_apply_fiscal_positions(self, *, company_id: int) -> tuple[FiscalPositionRecord, ...]:
        _require_positive_argument(company_id)
        records = self._adapter.search_read_all(
            model=FISCAL_POSITION_MODEL,
            domain=[["auto_apply", "=", True], ["company_id", "in", [company_id, False]]],
            fields=self._fields_for(FISCAL_POSITION_MODEL),
            max_records=MAX_FISCAL_POSITIONS + 1,
        )
        if len(records) > MAX_FISCAL_POSITIONS:
            raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR)
        positions = tuple(self._fiscal_position(record) for record in records)
        _require_unique_ids(position.id for position in positions)
        return positions

    def list_account_mappings(
        self, *, fiscal_position_ids: tuple[int, ...]
    ) -> tuple[FiscalPositionAccountMappingRecord, ...]:
        if not fiscal_position_ids:
            return ()
        for fiscal_position_id in fiscal_position_ids:
            _require_positive_argument(fiscal_position_id)
        records = self._adapter.search_read_all(
            model=FISCAL_POSITION_ACCOUNT_MODEL,
            domain=[["position_id", "in", sorted(set(fiscal_position_ids))]],
            fields=self._fields_for(FISCAL_POSITION_ACCOUNT_MODEL),
            max_records=MAX_FISCAL_POSITION_ACCOUNT_MAPPINGS + 1,
        )
        if len(records) > MAX_FISCAL_POSITION_ACCOUNT_MAPPINGS:
            raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR)
        mappings = tuple(self._mapping(record) for record in records)
        _require_unique_ids(mapping.id for mapping in mappings)
        return mappings

    def vendor_bill_fiscal_position_supported(self) -> bool:
        """Whether this Odoo exposes ``account.move.fiscal_position_id`` as expected (metadata only)."""

        return all(self._field_matches(MOVE_MODEL, spec) for spec in REQUIRED_FIELDS[MOVE_MODEL])

    def read_vendor_bill_fiscal_position_id(self, *, move_id: int, company_id: int) -> int | None:
        """The fiscal position on one created Vendor Bill (readback only); ``None`` when unset."""

        _require_positive_argument(move_id)
        _require_positive_argument(company_id)
        records = self._adapter.search_read(
            model=MOVE_MODEL,
            domain=[["id", "=", move_id], ["company_id", "=", company_id], ["move_type", "=", "in_invoice"]],
            fields=self._fields_for(MOVE_MODEL),
            limit=2,
        )
        if len(records) != 1:
            raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR)
        record = _record(records[0])
        if _required_positive_int(record.get("id")) != move_id:
            raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR)
        return _optional_many2one_id(record.get("fiscal_position_id"))

    # ------------------------------------------------------------------ record mapping

    def _fiscal_position(self, raw: object) -> FiscalPositionRecord:
        record = _record(raw)
        return _build(
            FiscalPositionRecord,
            id=_required_positive_int(record.get("id")),
            active=_required_bool(record.get("active")),
            company_id=_optional_many2one_id(record.get("company_id")),
            auto_apply=_required_bool(record.get("auto_apply")),
            account_mapping_ids=_required_id_list(record.get("account_ids")),
        )

    def _mapping(self, raw: object) -> FiscalPositionAccountMappingRecord:
        record = _record(raw)
        return _build(
            FiscalPositionAccountMappingRecord,
            id=_required_positive_int(record.get("id")),
            position_id=_required_many2one_id(record.get("position_id")),
            account_src_id=_required_many2one_id(record.get("account_src_id")),
            account_dest_id=_required_many2one_id(record.get("account_dest_id")),
        )

    # ------------------------------------------------------------------ metadata validation

    def _fields_for(self, model: str) -> list[str]:
        self._ensure_model_validated(model)
        return ["id", *(name for name, _ttype, _relation in REQUIRED_FIELDS[model])]

    def _ensure_model_validated(self, model: str) -> None:
        if model in self._validated_models:
            return
        for spec in REQUIRED_FIELDS[model]:
            if not self._field_matches(model, spec):
                raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR)
        self._validated_models.add(model)

    def _field_matches(self, model: str, spec: _FieldSpec) -> bool:
        field_name, ttype, relation = spec
        records = self._adapter.read_model_field_metadata(model=model, field_name=field_name)
        if len(records) != 1 or not isinstance(records[0], dict):
            return False
        metadata = records[0]
        if metadata.get("name") != field_name or metadata.get("ttype") != ttype:
            return False
        return relation is None or metadata.get("relation") == relation


def _build(dto_type: type, **values: Any) -> Any:
    try:
        return dto_type(**values)
    except Exception as exc:
        raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR) from exc


def _record(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR)
    return value


def _require_positive_argument(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR)


def _require_unique_ids(ids: Any) -> None:
    seen: set[int] = set()
    for record_id in ids:
        if record_id in seen:
            raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR)
        seen.add(record_id)


def _required_positive_int(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR)
    return value


def _required_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR)
    return value


def _optional_many2one_id(value: object) -> int | None:
    # Odoo's json/2 search_read represents an empty many2one as the literal ``False`` --
    # the only falsy shape treated as absence. Any other unrecognized shape fails closed.
    if value is False:
        return None
    return _required_many2one_id(value)


def _required_many2one_id(value: object) -> int:
    if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[1], str):
        return _required_positive_int(value[0])
    if type(value) is int and value > 0:
        return value
    raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR)


def _required_id_list(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ErpRepositoryResponseError(SAFE_FISCAL_POSITION_ERROR)
    return tuple(_required_positive_int(item) for item in value)


__all__ = [
    "FISCAL_POSITION_ACCOUNT_MODEL",
    "FISCAL_POSITION_MODEL",
    "REQUIRED_FIELDS",
    "SAFE_FISCAL_POSITION_ERROR",
    "OdooFiscalPositionReader",
]
