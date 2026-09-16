from __future__ import annotations

from typing import Any

from app.application.workbench.selected_expense_account_resolution import ResolutionAccountRecord
from app.erp.odoo.adapter import OdooReadOnlyAdapter

ACCOUNT_FIELDS = ["id", "company_ids"]


class OdooSelectedAccountReader:
    """Read-only ``account.account`` reader for explicit expense-account validation (P0-PROD-08G).

    Thin wrapper over the sanctioned read-only :class:`OdooReadOnlyAdapter`; it
    performs no write and adds no Odoo write capability -- only extends the
    client's existing read-only ``search_read`` model allowlist to include
    ``account.account``.
    """

    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_accounts_by_ids(self, account_ids: tuple[int, ...]) -> tuple[ResolutionAccountRecord, ...]:
        valid_ids = tuple(
            account_id
            for account_id in account_ids
            if type(account_id) is int and not isinstance(account_id, bool) and account_id > 0
        )
        if not valid_ids:
            return ()
        records = self._adapter.search_read_all(
            model="account.account",
            domain=[["id", "in", list(valid_ids)]],
            fields=ACCOUNT_FIELDS,
            max_records=len(valid_ids),
        )
        return tuple(_account(record) for record in records)


def _account(record: dict[str, Any]) -> ResolutionAccountRecord:
    return ResolutionAccountRecord(
        id=int(record["id"]),
        company_ids=_many2many_ids(record.get("company_ids")),
    )


def _many2many_ids(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if type(item) is int and not isinstance(item, bool))
