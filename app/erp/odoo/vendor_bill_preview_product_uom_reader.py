from __future__ import annotations

from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.write.exceptions import VendorBillWriteValidationError

PRODUCT_UOM_FIELDS = ["id", "uom_id"]


class OdooVendorBillPreviewProductUomReader:
    """Structurally read-only ``product.product`` UoM resolution for Vendor Bill
    preview (P0-PROD-10E).

    Mirrors ``AccountMoveRepository._resolve_vendor_bill_product_uoms`` exactly --
    same model, same fields, same "every product must resolve" fail-closed
    validation -- so a preview's UoM failure is indistinguishable from what EXECUTE
    would hit for the same invoice. Built on :class:`OdooReadOnlyAdapter`, which has
    no create/write/unlink method at all: this class has no path to an Odoo write,
    not merely a boolean check against one. Never derives anything from the source
    invoice's own UN/CEFACT unit code -- the resolved product itself is the only
    input.
    """

    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def resolve_vendor_bill_product_uom_ids(self, product_ids: tuple[int, ...]) -> dict[int, int]:
        if not product_ids:
            return {}
        records = self._adapter.search_read(
            model="product.product",
            domain=[["id", "in", list(product_ids)]],
            fields=PRODUCT_UOM_FIELDS,
            limit=len(product_ids),
        )
        resolved: dict[int, int] = {}
        for record in records:
            product_id = record.get("id")
            uom_id = _many2one_id(record.get("uom_id"))
            if isinstance(product_id, int) and not isinstance(product_id, bool) and uom_id is not None:
                resolved[product_id] = uom_id
        missing = sorted(set(product_ids) - resolved.keys())
        if missing:
            raise VendorBillWriteValidationError(
                f"Vendor Bill product UoM could not be resolved for Odoo product id(s): {missing}."
            )
        return resolved


def _many2one_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, list | tuple) and value and isinstance(value[0], int) and not isinstance(value[0], bool):
        return value[0]
    return None
