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


class SupplierPartnerClassificationUnavailableError(SupplierPartnerWriteError):
    """Raised before any partner create when the configured Odoo classification field is
    unset/malformed, missing in Odoo, not a selection, or lacks the required key."""

    error_category = "supplier_partner_classification_unavailable"


class SupplierPartnerProbableDuplicateError(SupplierPartnerWriteError):
    """Raised before any partner create when Odoo already holds a commercial company with
    the same canonical legal name whose VAT is missing or not a valid VKN/TCKN.

    Never resolved automatically: the operator corrects that company's VAT in Odoo first.
    ``safe_message`` is operator-facing Turkish text; ``candidate_partner_ids`` lists every
    blocking company (several are never disambiguated).
    """

    error_category = "supplier_partner_probable_duplicate"

    def __init__(self, safe_message: str, *, candidate_partner_ids: tuple[int, ...]) -> None:
        super().__init__(safe_message)
        self.candidate_partner_ids = candidate_partner_ids
