from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.tax_mapping.result import TaxType


@dataclass(frozen=True, slots=True)
class TaxCandidate:
    tax_id: int
    company_id: int | None
    tax_type: TaxType
    rate: Decimal
    active: bool = True
    usage_type: str | None = None


class TaxRepository(Protocol):
    def find_candidates(
        self,
        *,
        company_id: int | None,
        rate: Decimal,
        tax_type: TaxType,
    ) -> Sequence[TaxCandidate]:
        pass
