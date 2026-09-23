"""P0-PROD-15P: read-only ``account.account`` lookup for operating-expense mapping selection.

Closes the gap identified in P0-PROD-15O: an operator configuring an
``OperatingExpenseMapping`` had no supported way to discover a valid Odoo expense
account id. This is deliberately not a generic chart-of-accounts browser -- the
target model (``account.account``), the base domain (company scope + eligible
account type, deprecated accounts excluded when that field is available), and the
field list are all fixed here, never caller-controlled. The only caller input is an
optional, narrowly-validated free-text ``query`` that only ever appears inside an
``ilike`` domain leaf, never as raw search syntax.

Field existence is verified at runtime via the sanctioned ``ir.model.fields``
metadata read (:meth:`OdooReadOnlyAdapter.read_model_field_metadata`) before any of
``code``/``name``/``account_type`` is ever requested -- P0-PROD-15J/15K proved this
production Odoo instance can reject a field a generic Odoo schema would normally
have, and this module must never repeat that mistake by assumption.
"""

from __future__ import annotations

from typing import Any

from app.application.workbench.expense_account_lookup import ExpenseAccountCandidate
from app.erp.exceptions import ErpRepositoryResponseError
from app.erp.odoo.adapter import OdooReadOnlyAdapter

SAFE_EXPENSE_ACCOUNT_LOOKUP_ERROR = "Odoo expense account lookup returned an unsafe response."

ACCOUNT_MODEL = "account.account"

#: Eligible for an *operating*-expense mapping specifically -- deliberately narrower than
#: every P&L expense-shaped type (e.g. excludes "expense_direct_cost"/COGS, a distinct
#: accounting concept). Extending this is a code change to this module, never a runtime
#: decision.
ELIGIBLE_ACCOUNT_TYPES = ("expense",)

#: Always requested once existence is confirmed via metadata. Fixed, not caller-configurable.
_REQUIRED_METADATA_VALIDATED_FIELDS = ("code", "name", "account_type")
_OPTIONAL_METADATA_VALIDATED_FIELDS = ("deprecated",)


class OdooExpenseAccountCandidateReader:
    """Structurally read-only ``account.account`` reader, always company- and type-scoped.

    Built on :class:`OdooReadOnlyAdapter`, which exposes only
    ``search_read``/``search_read_all``/``read_model_field_metadata`` and structurally
    cannot create, write, unlink, post, pay, or reconcile anything.
    """

    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter
        self._fields_validated = False
        self._deprecated_available: bool | None = None

    def find_candidates(self, *, company_id: int, query: str | None) -> tuple[ExpenseAccountCandidate, ...]:
        if type(company_id) is not int or isinstance(company_id, bool) or company_id <= 0:
            raise ErpRepositoryResponseError(SAFE_EXPENSE_ACCOUNT_LOOKUP_ERROR)
        records = self._search(company_id=company_id, extra_domain=_query_domain(query))
        return tuple(_candidate_from_record(record) for record in records)

    def find_eligible_by_id(self, *, company_id: int, account_id: int) -> ExpenseAccountCandidate | None:
        if type(company_id) is not int or isinstance(company_id, bool) or company_id <= 0:
            raise ErpRepositoryResponseError(SAFE_EXPENSE_ACCOUNT_LOOKUP_ERROR)
        if type(account_id) is not int or isinstance(account_id, bool) or account_id <= 0:
            raise ErpRepositoryResponseError(SAFE_EXPENSE_ACCOUNT_LOOKUP_ERROR)
        records = self._search(company_id=company_id, extra_domain=[["id", "=", account_id]])
        if not records:
            return None
        if len(records) > 1:
            raise ErpRepositoryResponseError(SAFE_EXPENSE_ACCOUNT_LOOKUP_ERROR)
        candidate = _candidate_from_record(records[0])
        if candidate.id != account_id:
            raise ErpRepositoryResponseError(SAFE_EXPENSE_ACCOUNT_LOOKUP_ERROR)
        return candidate

    def _search(self, *, company_id: int, extra_domain: list[Any]) -> tuple[dict[str, Any], ...]:
        self._ensure_required_fields_validated()
        domain: list[Any] = [
            ["company_ids", "in", [company_id]],
            ["account_type", "in", list(ELIGIBLE_ACCOUNT_TYPES)],
        ]
        if self._is_deprecated_field_available():
            domain.append(["deprecated", "=", False])
        domain.extend(extra_domain)
        fields = ["id", "code", "name", "account_type", "company_ids"]
        return self._adapter.search_read_all(model=ACCOUNT_MODEL, domain=domain, fields=fields, max_records=500)

    def _ensure_required_fields_validated(self) -> None:
        if self._fields_validated:
            return
        for field_name in _REQUIRED_METADATA_VALIDATED_FIELDS:
            if not self._field_exists(field_name):
                raise ErpRepositoryResponseError(SAFE_EXPENSE_ACCOUNT_LOOKUP_ERROR)
        self._fields_validated = True

    def _is_deprecated_field_available(self) -> bool:
        if self._deprecated_available is None:
            self._deprecated_available = self._field_exists(_OPTIONAL_METADATA_VALIDATED_FIELDS[0])
        return self._deprecated_available

    def _field_exists(self, field_name: str) -> bool:
        records = self._adapter.read_model_field_metadata(model=ACCOUNT_MODEL, field_name=field_name)
        return len(records) == 1 and records[0].get("name") == field_name


def _query_domain(query: str | None) -> list[Any]:
    if query is None:
        return []
    return [["|", ["code", "ilike", query], ["name", "ilike", query]]]


def _candidate_from_record(record: object) -> ExpenseAccountCandidate:
    if not isinstance(record, dict):
        raise ErpRepositoryResponseError(SAFE_EXPENSE_ACCOUNT_LOOKUP_ERROR)
    return ExpenseAccountCandidate(
        id=_required_positive_int(record.get("id")),
        code=_required_text(record.get("code")),
        name=_required_text(record.get("name")),
        account_type=_required_text(record.get("account_type")),
    )


def _required_positive_int(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or value <= 0:
        raise ErpRepositoryResponseError(SAFE_EXPENSE_ACCOUNT_LOOKUP_ERROR)
    return value


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ErpRepositoryResponseError(SAFE_EXPENSE_ACCOUNT_LOOKUP_ERROR)
    return value


__all__ = ["OdooExpenseAccountCandidateReader"]
