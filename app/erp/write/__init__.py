"""ERP write adapters for controlled draft-only operations."""

from app.erp.write.account_move_repository import AccountMoveDraft, AccountMoveRepository
from app.erp.write.exceptions import (
    CustomerInvoiceWriteAuthenticationError,
    CustomerInvoiceWriteAuthorizationError,
    CustomerInvoiceWriteDuplicateError,
    CustomerInvoiceWriteError,
    CustomerInvoiceWriteSafetyGateError,
    CustomerInvoiceWriteTransportError,
    CustomerInvoiceWriteUnexpectedErpError,
    CustomerInvoiceWriteValidationError,
    VendorBillWriteAuthenticationError,
    VendorBillWriteAuthorizationError,
    VendorBillWriteDuplicateError,
    VendorBillWriteError,
    VendorBillWriteSafetyGateError,
    VendorBillWriteTransportError,
    VendorBillWriteUnexpectedErpError,
    VendorBillWriteValidationError,
)
from app.erp.write.odoo_customer_invoice_writer import OdooCustomerInvoiceWritePolicy, OdooCustomerInvoiceWriter
from app.erp.write.odoo_vendor_bill_writer import OdooVendorBillWritePolicy, OdooVendorBillWriter

__all__ = [
    "AccountMoveDraft",
    "AccountMoveRepository",
    "CustomerInvoiceWriteAuthenticationError",
    "CustomerInvoiceWriteAuthorizationError",
    "CustomerInvoiceWriteDuplicateError",
    "CustomerInvoiceWriteError",
    "CustomerInvoiceWriteSafetyGateError",
    "CustomerInvoiceWriteTransportError",
    "CustomerInvoiceWriteUnexpectedErpError",
    "CustomerInvoiceWriteValidationError",
    "OdooCustomerInvoiceWritePolicy",
    "OdooCustomerInvoiceWriter",
    "OdooVendorBillWritePolicy",
    "OdooVendorBillWriter",
    "VendorBillWriteAuthenticationError",
    "VendorBillWriteAuthorizationError",
    "VendorBillWriteDuplicateError",
    "VendorBillWriteError",
    "VendorBillWriteSafetyGateError",
    "VendorBillWriteTransportError",
    "VendorBillWriteUnexpectedErpError",
    "VendorBillWriteValidationError",
]
