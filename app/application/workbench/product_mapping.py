"""PRODUCT_NOT_FOUND operator flow: map one invoice line to an EXISTING Odoo product.

The operator names one review line and one existing ``product.product``. The Hub
records that explicit human decision as supplier-specific master data -- one Odoo
``product.supplierinfo`` row ``(supplier partner, seller product code) -> product`` --
through the existing guarded ``SupplierInfoWriter``, then deterministically
reclassifies the review (``MASTER_DATA_CHANGED``). The normal product matcher already
resolves ``(matched supplier, seller_item_code) -> supplierinfo -> variant``, so the
line matches now and every future invoice from the same supplier with the same seller
code matches automatically.

Never trusted from the operator: the seller product code (immutable source line), the
supplier partner (this version's deterministic supplier match -- exactly the partner
the matcher will use when reclassifying) and the line identity
``(review_id, company_id, review_version, line_number)``. Nothing is ever overwritten:
an existing mapping of the same supplier/code to a different product fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import (
    ProductRemediationConflictError,
    ProductRemediationContractError,
    ProductRemediationEligibilityError,
)


class ProductMappingSellerCodeMissingError(ProductRemediationEligibilityError):
    """The source line has no seller product code: a supplier-specific mapping key cannot exist."""

    error_category = "product_mapping_seller_code_missing"


class ProductMappingProductInvalidError(ProductRemediationEligibilityError):
    """The selected Odoo product is missing, archived, in another company or has no template."""

    error_category = "product_mapping_product_invalid"


class ProductMappingConflictError(ProductRemediationConflictError):
    """The supplier/seller-code identity is already mapped (or being created) elsewhere."""

    error_category = "product_mapping_conflict"


@dataclass(frozen=True, slots=True)
class MapExistingProductCommand(ApplicationDTO):
    review_id: str
    company_id: int
    expected_version: int
    line_number: str
    product_id: int
    approved_by: str
    authorization_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.review_id, str) or not self.review_id.strip():
            raise ProductRemediationContractError("review_id is required.")
        for value, label in (
            (self.company_id, "company_id"),
            (self.expected_version, "expected_version"),
            (self.product_id, "product_id"),
        ):
            if type(value) is not int or value <= 0:
                raise ProductRemediationContractError(f"{label} must be positive.")
        if not isinstance(self.line_number, str) or not self.line_number.strip():
            raise ProductRemediationContractError("Fatura satırı seçilmelidir.")
        if not isinstance(self.approved_by, str) or not self.approved_by.strip():
            raise ProductRemediationContractError("approved_by (authenticated actor) is required.")
        if self.authorization_id is not None and (
            not isinstance(self.authorization_id, str) or not self.authorization_id.strip()
        ):
            raise ProductRemediationContractError("authorization_id must be non-empty text when provided.")


@dataclass(frozen=True, slots=True)
class MapExistingProductResult(ApplicationDTO):
    review_id: str
    company_id: int
    line_number: str
    seller_item_code: str
    supplier_partner_id: int
    product_id: int
    product_name: str | None
    supplierinfo_id: int | None
    created_supplierinfo: bool
    previous_version: int
    current_version: int
    #: The mapped line no longer carries PRODUCT_NOT_FOUND after reclassification.
    line_resolved: bool
    #: Lines still carrying PRODUCT_NOT_FOUND after reclassification (other lines).
    remaining_product_lines: tuple[str, ...] = field(default_factory=tuple)


__all__ = [
    "MapExistingProductCommand",
    "MapExistingProductResult",
    "ProductMappingConflictError",
    "ProductMappingProductInvalidError",
    "ProductMappingSellerCodeMissingError",
]
