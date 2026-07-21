from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from app.erp.models import Partner


class PartnerRepository(Protocol):
    def find_by_tax_number(self, tax_number: str, *, company_id: int | None = None) -> Sequence[Partner]:
        pass

    def find_by_ids(self, ids: Sequence[int]) -> Sequence[Partner]:
        pass
