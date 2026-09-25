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
    company_type: str | None = None
    parent_id: int | None = None
    commercial_partner_id: int | None = None
    street: str | None = None
    street2: str | None = None
    zip_code: str | None = None
    city: str | None = None
    state_id: int | None = None
    country_id: int | None = None
    email: str | None = None
    phone: str | None = None
    mobile: str | None = None
    website: str | None = None
    supplier_rank: int | None = None
    customer_rank: int | None = None


@dataclass(frozen=True, slots=True)
class Product:
    id: int
    name: str | None
    default_code: str | None
    barcode: str | None
    active: bool
    company_id: int | None = None


@dataclass(frozen=True, slots=True)
class SupplierProductCode:
    """Read-only projection of one ``product.supplierinfo`` row (P0-PROD-19A-3).

    ``product_id`` is the explicit variant when set; ``None`` means the row is
    template-level (Odoo applies it to every variant of ``product_tmpl_id``).
    """

    id: int
    partner_id: int
    product_code: str
    product_tmpl_id: int
    product_id: int | None
    company_id: int | None


@dataclass(frozen=True, slots=True)
class ProductVariant:
    id: int
    product_tmpl_id: int
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
    tax_number: str | None = None
