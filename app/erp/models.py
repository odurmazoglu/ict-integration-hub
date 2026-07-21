from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.tax_mapping.result import TaxType


@dataclass(frozen=True, slots=True)
class Partner:
    id: int
    name: str | None
    tax_number: str | None
    active: bool
    company_id: int | None = None


@dataclass(frozen=True, slots=True)
class Product:
    id: int
    name: str | None
    default_code: str | None
    barcode: str | None
    active: bool
    company_id: int | None = None


@dataclass(frozen=True, slots=True)
class Tax:
    id: int
    company_id: int | None
    tax_type: TaxType
    rate: Decimal
    active: bool
    usage_type: str | None = None


@dataclass(frozen=True, slots=True)
class Currency:
    id: int
    code: str
    active: bool


@dataclass(frozen=True, slots=True)
class Company:
    id: int
    name: str | None
