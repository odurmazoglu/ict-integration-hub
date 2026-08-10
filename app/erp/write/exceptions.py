from __future__ import annotations

from app.application.exceptions import ApplicationError


class VendorBillWriteError(ApplicationError):
    error_category = "vendor_bill_write_error"


class VendorBillWriteAuthenticationError(VendorBillWriteError):
    error_category = "authentication_failure"


class VendorBillWriteAuthorizationError(VendorBillWriteError):
    error_category = "authorization_failure"


class VendorBillWriteValidationError(VendorBillWriteError):
    error_category = "validation_failure"


class VendorBillWriteSafetyGateError(VendorBillWriteValidationError):
    error_category = "production_safety_gate_failure"


class VendorBillWriteTransportError(VendorBillWriteError):
    error_category = "transport_failure"


class VendorBillWriteDuplicateError(VendorBillWriteError):
    error_category = "duplicate_detection_failure"


class VendorBillWriteUnexpectedErpError(VendorBillWriteError):
    error_category = "unexpected_erp_error"


class CustomerInvoiceWriteError(ApplicationError):
    error_category = "customer_invoice_write_error"


class CustomerInvoiceWriteAuthenticationError(CustomerInvoiceWriteError):
    error_category = "authentication_failure"


class CustomerInvoiceWriteAuthorizationError(CustomerInvoiceWriteError):
    error_category = "authorization_failure"


class CustomerInvoiceWriteValidationError(CustomerInvoiceWriteError):
    error_category = "validation_failure"


class CustomerInvoiceWriteSafetyGateError(CustomerInvoiceWriteValidationError):
    error_category = "production_safety_gate_failure"


class CustomerInvoiceWriteTransportError(CustomerInvoiceWriteError):
    error_category = "transport_failure"


class CustomerInvoiceWriteDuplicateError(CustomerInvoiceWriteError):
    error_category = "duplicate_detection_failure"


class CustomerInvoiceWriteUnexpectedErpError(CustomerInvoiceWriteError):
    error_category = "unexpected_erp_error"
