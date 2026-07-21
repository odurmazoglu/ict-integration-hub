from __future__ import annotations

from typing import Any

from app.erp.models import Currency
from app.erp.odoo.adapter import OdooReadOnlyAdapter

CURRENCY_FIELDS = ["id", "name", "active"]


class OdooCurrencyRepository:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_by_code(self, code: str) -> Currency | None:
        records = self._adapter.search_read(
            model="res.currency",
            domain=[["name", "=", code.upper()]],
            fields=CURRENCY_FIELDS,
            limit=1,
        )
        if not records:
            return None
        return _currency(records[0])


def _currency(record: dict[str, Any]) -> Currency:
    return Currency(id=int(record["id"]), code=str(record["name"]), active=bool(record.get("active", True)))
