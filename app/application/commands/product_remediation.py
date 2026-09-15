from __future__ import annotations

from dataclasses import dataclass

from app.application.commands.base import Command
from app.application.exceptions.product_remediation import (
    ProductWriteValidationError,
    SupplierInfoWriteValidationError,
)

# Odoo v19 product.template.type selection. Combo products are out of scope for the
# simple no-attribute CREATE_NEW_PRODUCT v1 flow but are accepted here since the
# gate/schema capability is generic; the future remediation flow chooses the value.
ALLOWED_PRODUCT_TEMPLATE_TYPES = frozenset({"consu", "service", "combo"})


@dataclass(frozen=True, slots=True)
class CreateProductCommand(Command):
    """Application request to create one Odoo ``product.template`` from operator-supplied identity.

    Only the exact fields required for a simple no-attribute product are carried.
    ``default_code`` is operator-controlled or blank -- it is never derived from a
    supplier's own product code (see ``CreateSupplierInfoCommand.product_code``).
    No category, taxes, barcode, or company are set here; those are left to
    documented Odoo defaults.
    """

    name: str
    type: str
    uom_id: int
    is_storable: bool
    default_code: str | None = None
    approved_by: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ProductWriteValidationError("name is required.")
        if self.type not in ALLOWED_PRODUCT_TEMPLATE_TYPES:
            raise ProductWriteValidationError("type must be a recognized Odoo product.template type.")
        if type(self.uom_id) is not int or isinstance(self.uom_id, bool) or self.uom_id <= 0:
            raise ProductWriteValidationError("A positive uom_id is required.")
        if not isinstance(self.is_storable, bool):
            raise ProductWriteValidationError("is_storable must be a boolean.")
        if self.default_code is not None and (not isinstance(self.default_code, str) or not self.default_code.strip()):
            raise ProductWriteValidationError("default_code must be a non-empty string when provided.")
        if self.approved_by is not None and (not isinstance(self.approved_by, str) or not self.approved_by.strip()):
            raise ProductWriteValidationError("approved_by must be a non-empty name when provided.")


@dataclass(frozen=True, slots=True)
class CreateSupplierInfoCommand(Command):
    """Application request to create one Odoo ``product.supplierinfo`` link.

    Supplier identity (``product_code``) is strictly separate from ICT product
    identity (``product.template.default_code``): this command never writes to,
    and the writer must never derive, ``default_code`` from ``product_code``.

    ``currency_id``, ``delay``, ``min_qty``, and ``price`` are left as Odoo
    defaults (omitted from the write payload) unless explicitly supplied.
    """

    company_id: int
    partner_id: int
    product_tmpl_id: int
    product_code: str
    idempotency_key: str
    product_id: int | None = None
    product_name: str | None = None
    currency_id: int | None = None
    delay: int | None = None
    min_qty: float | None = None
    price: float | None = None
    approved_by: str | None = None

    def __post_init__(self) -> None:
        if type(self.company_id) is not int or isinstance(self.company_id, bool) or self.company_id <= 0:
            raise SupplierInfoWriteValidationError("A positive company_id is required.")
        if type(self.partner_id) is not int or isinstance(self.partner_id, bool) or self.partner_id <= 0:
            raise SupplierInfoWriteValidationError("A positive partner_id is required.")
        if type(self.product_tmpl_id) is not int or isinstance(self.product_tmpl_id, bool) or self.product_tmpl_id <= 0:
            raise SupplierInfoWriteValidationError("A positive product_tmpl_id is required.")
        if not isinstance(self.product_code, str) or not self.product_code.strip():
            raise SupplierInfoWriteValidationError("product_code is required.")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise SupplierInfoWriteValidationError("idempotency_key is required.")
        if self.product_id is not None and (
            type(self.product_id) is not int or isinstance(self.product_id, bool) or self.product_id <= 0
        ):
            raise SupplierInfoWriteValidationError("product_id must be a positive Odoo id when provided.")
        if self.product_name is not None and (not isinstance(self.product_name, str) or not self.product_name.strip()):
            raise SupplierInfoWriteValidationError("product_name must be a non-empty string when provided.")
        if self.currency_id is not None and (
            type(self.currency_id) is not int or isinstance(self.currency_id, bool) or self.currency_id <= 0
        ):
            raise SupplierInfoWriteValidationError("currency_id must be a positive Odoo id when provided.")
        if self.delay is not None and (type(self.delay) is not int or isinstance(self.delay, bool)):
            raise SupplierInfoWriteValidationError("delay must be an integer number of days when provided.")
        if self.min_qty is not None and (
            not isinstance(self.min_qty, int | float) or isinstance(self.min_qty, bool) or self.min_qty < 0
        ):
            raise SupplierInfoWriteValidationError("min_qty must be a non-negative number when provided.")
        if self.price is not None and (
            not isinstance(self.price, int | float) or isinstance(self.price, bool) or self.price < 0
        ):
            raise SupplierInfoWriteValidationError("price must be a non-negative number when provided.")
        if self.approved_by is not None and (not isinstance(self.approved_by, str) or not self.approved_by.strip()):
            raise SupplierInfoWriteValidationError("approved_by must be a non-empty name when provided.")
