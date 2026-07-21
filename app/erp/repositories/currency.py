from __future__ import annotations

from typing import Protocol

from app.erp.models import Currency


class CurrencyRepository(Protocol):
    def find_by_code(self, code: str) -> Currency | None:
        pass
