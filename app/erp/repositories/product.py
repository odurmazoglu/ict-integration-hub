from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from app.erp.models import Product


class ProductRepository(Protocol):
    def find_by_default_code(self, default_code: str, *, company_id: int | None = None) -> Sequence[Product]:
        pass

    def find_by_barcode(self, barcode: str, *, company_id: int | None = None) -> Sequence[Product]:
        pass

    def find_by_ids(self, ids: Sequence[int]) -> Sequence[Product]:
        pass
