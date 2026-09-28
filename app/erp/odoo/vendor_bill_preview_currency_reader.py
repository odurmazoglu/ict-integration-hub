from __future__ import annotations

from app.application.execution.vendor_bill_preview import VendorBillCurrency
from app.billing.money import MAX_CURRENCY_DECIMAL_PLACES
from app.erp.exceptions import ErpRepositoryError, ErpRepositoryTimeoutError
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.write.exceptions import (
    VendorBillWriteTransportError,
    VendorBillWriteUnexpectedErpError,
    VendorBillWriteValidationError,
)

# P0-PROD-19E-1: decimal_places is the currency's own monetary precision, read in the
# same single call -- the Hub never assumes a currency has two decimals.
CURRENCY_FIELDS = ["id", "name", "active", "decimal_places"]


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

    def resolve_vendor_bill_currency(self, currency_code: str) -> VendorBillCurrency:
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
        decimal_places = record.get("decimal_places")
        if (
            type(decimal_places) is not int
            or isinstance(decimal_places, bool)
            or not 0 <= decimal_places <= MAX_CURRENCY_DECIMAL_PLACES
        ):
            raise VendorBillWriteValidationError("Vendor Bill currency precision is not readable from Odoo.")
        return VendorBillCurrency(currency_id=currency_id, decimal_places=decimal_places)


class OdooVendorBillExecutionCurrencyReader:
    """The same read-only currency resolution for EXECUTE's pre-write monetary gate
    (P0-PROD-19E-2), with read failures classified exactly as the writer's own currency
    lookup classifies them (``_translate_connector_errors``): a timeout stays a retryable
    transport failure, any other ERP read failure an unexpected ERP error.
    """

    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._reader = OdooVendorBillPreviewCurrencyReader(adapter=adapter)

    def resolve_vendor_bill_currency(self, currency_code: str) -> VendorBillCurrency:
        try:
            return self._reader.resolve_vendor_bill_currency(currency_code)
        except ErpRepositoryTimeoutError as exc:
            raise VendorBillWriteTransportError(exc.safe_message) from exc
        except ErpRepositoryError as exc:
            raise VendorBillWriteUnexpectedErpError(exc.safe_message) from exc
