from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.application.dto.base import ApplicationDTO
from app.application.exceptions.product_remediation import (
    ProductDataIntegrityError,
    SupplierInfoDataIntegrityError,
)


class ProductWriteStatus(StrEnum):
    """Terminal outcome of a controlled product.template write request."""

    CREATED = "created"


@dataclass(frozen=True, slots=True)
class ProductWriteResult(ApplicationDTO):
    """Typed result of a controlled Odoo ``product.template`` write.

    Carries the stable identity (``template_id`` and the resolved single
    ``product_id`` variant) that a later remediation orchestration needs to
    resume safely if a subsequent ``product.supplierinfo`` write fails --
    the two writes are not one Odoo transaction.
    """

    status: ProductWriteStatus
    template_id: int
    product_id: int
    name: str
    default_code: str | None
    safe_message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ProductWriteStatus):
            raise ProductDataIntegrityError("A canonical product write status is required.")
        if type(self.template_id) is not int or self.template_id <= 0:
            raise ProductDataIntegrityError("template_id must be a positive Odoo id.")
        if type(self.product_id) is not int or self.product_id <= 0:
            raise ProductDataIntegrityError("product_id must be a positive Odoo id.")


class SupplierInfoWriteStatus(StrEnum):
    """Terminal outcome of a controlled product.supplierinfo write request."""

    CREATED = "created"
    ALREADY_EXISTS = "already_exists"


@dataclass(frozen=True, slots=True)
class SupplierInfoWriteResult(ApplicationDTO):
    """Typed result of a controlled Odoo ``product.supplierinfo`` write."""

    status: SupplierInfoWriteStatus
    supplierinfo_id: int
    partner_id: int
    product_tmpl_id: int
    product_code: str
    company_id: int | None
    idempotency_key: str
    product_id: int | None = None
    safe_message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, SupplierInfoWriteStatus):
            raise SupplierInfoDataIntegrityError("A canonical supplierinfo write status is required.")
        if type(self.supplierinfo_id) is not int or self.supplierinfo_id <= 0:
            raise SupplierInfoDataIntegrityError("supplierinfo_id must be a positive Odoo id.")
        if type(self.partner_id) is not int or self.partner_id <= 0:
            raise SupplierInfoDataIntegrityError("partner_id must be a positive Odoo id.")
        if type(self.product_tmpl_id) is not int or self.product_tmpl_id <= 0:
            raise SupplierInfoDataIntegrityError("product_tmpl_id must be a positive Odoo id.")
