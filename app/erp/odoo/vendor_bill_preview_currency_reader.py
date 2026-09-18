from __future__ import annotations

from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.write.exceptions import VendorBillWriteValidationError

CURRENCY_FIELDS = ["id", "name", "active"]


class OdooVendorBillPreviewCurrencyReader:
    """Structurally read-only ``res.currency`` resolution for Vendor Bill preview
    (P0-PROD-09B).

    Mirrors ``AccountMoveRepository._resolve_vendor_bill_currency`` exactly -- same
    domain filter, same "exactly one active exact match" validation, same exception
    type -- so a preview's currency failure is indistinguishable from what EXECUTE
    would hit for the same invoice. Built on :class:`OdooReadOnlyAdapter`, which has
    no create/write/unlink method at all: this class has no path to an Odoo write,
    not merely a boolean check against one.
    """

    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def resolve_vendor_bill_currency_id(self, currency_code: str) -> int:
        code = currency_code.strip().upper() if isinstance(currency_code, str) else ""
        if not code:
            raise VendorBillWriteValidationError("Vendor Bill currency code is required.")
        records = self._adapter.search_read(
            model="res.currency",
            domain=[["name", "=", code], ["active", "in", [True, False]]],
            fields=CURRENCY_FIELDS,
            limit=2,
        )
        if len(records) != 1:
            raise VendorBillWriteValidationError("Vendor Bill currency must resolve to exactly one Odoo currency.")
        record = records[0]
        currency_id = record.get("id")
        if (
            type(currency_id) is not int
            or isinstance(currency_id, bool)
            or currency_id <= 0
            or str(record.get("name", "")).strip().upper() != code
            or record.get("active") is not True
        ):
            raise VendorBillWriteValidationError("Vendor Bill currency is not an active exact Odoo currency.")
        return currency_id
