from __future__ import annotations

from app.application.exceptions.base import ApplicationError


class SupplierPartnerWriteError(ApplicationError):
    """Safe base error for controlled Odoo supplier partner master-data writes."""

    error_category = "supplier_partner_write_error"


class SupplierPartnerWriteAuthenticationError(SupplierPartnerWriteError):
    error_category = "authentication_failure"


class SupplierPartnerWriteAuthorizationError(SupplierPartnerWriteError):
    error_category = "authorization_failure"


class SupplierPartnerWriteValidationError(SupplierPartnerWriteError):
    error_category = "validation_failure"


class SupplierPartnerWriteSafetyGateError(SupplierPartnerWriteValidationError):
    error_category = "production_safety_gate_failure"


class SupplierPartnerWriteTransportError(SupplierPartnerWriteError):
    error_category = "transport_failure"


class SupplierPartnerAmbiguityError(SupplierPartnerWriteError):
    """Raised when more than one exact-VAT ``res.partner`` exists; fail closed, never pick one."""

    error_category = "supplier_partner_ambiguity"


class SupplierPartnerDuplicateRaceError(SupplierPartnerAmbiguityError):
    """Raised when a post-create exact-VAT re-query finds more than one partner."""

    error_category = "supplier_partner_duplicate_race"


class SupplierPartnerInactiveError(SupplierPartnerWriteError):
    """Raised when the single exact-VAT partner is archived/inactive; never auto-reactivate."""

    error_category = "supplier_partner_inactive"


class SupplierPartnerDataIntegrityError(SupplierPartnerWriteError):
    """Raised when Odoo returns a partner that fails identity / company / read-back validation."""

    error_category = "supplier_partner_data_integrity_error"


class SupplierPartnerWriteUnexpectedErpError(SupplierPartnerWriteError):
    error_category = "unexpected_erp_error"
