from __future__ import annotations

from app.application.exceptions.base import ApplicationError


class ProductWriteError(ApplicationError):
    """Safe base error for controlled Odoo ``product.template`` master-data writes."""

    error_category = "product_write_error"


class ProductWriteAuthenticationError(ProductWriteError):
    error_category = "authentication_failure"


class ProductWriteAuthorizationError(ProductWriteError):
    error_category = "authorization_failure"


class ProductWriteValidationError(ProductWriteError):
    error_category = "validation_failure"


class ProductWriteSafetyGateError(ProductWriteValidationError):
    error_category = "production_safety_gate_failure"


class ProductWriteTransportError(ProductWriteError):
    error_category = "transport_failure"


class ProductDataIntegrityError(ProductWriteError):
    """Raised when Odoo returns a template/variant that fails read-back validation."""

    error_category = "product_data_integrity_error"


class ProductVariantResolutionError(ProductDataIntegrityError):
    """Raised when a created template resolves to zero or more than one variant."""

    error_category = "product_variant_resolution_error"


class ProductWriteUnexpectedErpError(ProductWriteError):
    error_category = "unexpected_erp_error"


class SupplierInfoWriteError(ApplicationError):
    """Safe base error for controlled Odoo ``product.supplierinfo`` master-data writes."""

    error_category = "supplier_info_write_error"


class SupplierInfoWriteAuthenticationError(SupplierInfoWriteError):
    error_category = "authentication_failure"


class SupplierInfoWriteAuthorizationError(SupplierInfoWriteError):
    error_category = "authorization_failure"


class SupplierInfoWriteValidationError(SupplierInfoWriteError):
    error_category = "validation_failure"


class SupplierInfoWriteTransportError(SupplierInfoWriteError):
    error_category = "transport_failure"


class SupplierInfoAmbiguityError(SupplierInfoWriteError):
    """Raised when more than one supplierinfo shares this vendor/product-code identity."""

    error_category = "supplier_info_ambiguity"


class SupplierInfoDuplicateRaceError(SupplierInfoAmbiguityError):
    """Raised when a post-create identity re-query finds more than one supplierinfo."""

    error_category = "supplier_info_duplicate_race"


class SupplierInfoDataIntegrityError(SupplierInfoWriteError):
    """Raised when Odoo returns a supplierinfo that fails identity / read-back validation."""

    error_category = "supplier_info_data_integrity_error"


class SupplierInfoWriteUnexpectedErpError(SupplierInfoWriteError):
    error_category = "unexpected_erp_error"
