"""Application-layer exception types."""

from app.application.exceptions.base import ApplicationError
from app.application.exceptions.supplier_partner import (
    SupplierPartnerAmbiguityError,
    SupplierPartnerDataIntegrityError,
    SupplierPartnerDuplicateRaceError,
    SupplierPartnerInactiveError,
    SupplierPartnerWriteAuthenticationError,
    SupplierPartnerWriteAuthorizationError,
    SupplierPartnerWriteError,
    SupplierPartnerWriteSafetyGateError,
    SupplierPartnerWriteTransportError,
    SupplierPartnerWriteUnexpectedErpError,
    SupplierPartnerWriteValidationError,
)

__all__ = [
    "ApplicationError",
    "SupplierPartnerAmbiguityError",
    "SupplierPartnerDataIntegrityError",
    "SupplierPartnerDuplicateRaceError",
    "SupplierPartnerInactiveError",
    "SupplierPartnerWriteAuthenticationError",
    "SupplierPartnerWriteAuthorizationError",
    "SupplierPartnerWriteError",
    "SupplierPartnerWriteSafetyGateError",
    "SupplierPartnerWriteTransportError",
    "SupplierPartnerWriteUnexpectedErpError",
    "SupplierPartnerWriteValidationError",
]
