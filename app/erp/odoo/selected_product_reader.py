from __future__ import annotations

from app.application.workbench.selected_product_resolution import ResolutionProductRecord
from app.erp.odoo.product_repository import OdooProductRepository


class OdooSelectedProductReader:
    """Read-only ``product.product`` reader for explicit selected-product validation.

    Thin wrapper over the sanctioned read-only :class:`OdooProductRepository`; it
    performs no write and adds no new Odoo model access.
    """

    def __init__(self, *, product_repository: OdooProductRepository) -> None:
        self._product_repository = product_repository

    def find_products_by_ids(self, product_ids: tuple[int, ...]) -> tuple[ResolutionProductRecord, ...]:
        valid_ids = tuple(
            product_id
            for product_id in product_ids
            if type(product_id) is int and not isinstance(product_id, bool) and product_id > 0
        )
        if not valid_ids:
            return ()
        products = self._product_repository.find_by_ids(valid_ids)
        return tuple(
            ResolutionProductRecord(
                id=product.id,
                name=product.name,
                default_code=product.default_code,
                barcode=product.barcode,
                active=product.active,
                company_id=product.company_id,
            )
            for product in products
        )
