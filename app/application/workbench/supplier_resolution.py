from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import SupplierResolutionContractError

SUPPLIER_RESOLUTION_NOTE_MAX_LENGTH = 1024


class SupplierResolutionMode(StrEnum):
    """Business-policy choices for resolving a review whose supplier is not matched.

    This is a *policy* enum -- not HTTP terminology and not an ``res.partner``
    implementation detail. ``SUPPLIER_NOT_FOUND`` never implies any of these
    automatically; a human/system must choose one explicitly.
    """

    MATCH_EXISTING = "match_existing"
    USE_ONE_OFF_SUPPLIER = "use_one_off_supplier"
    CREATE_PERMANENT_SUPPLIER = "create_permanent_supplier"


class SupplierResolutionValidationStatus(StrEnum):
    """Outcome of validating a chosen :class:`SupplierResolution`."""

    #: MATCH_EXISTING passed every check; ``effective_partner_id`` is set.
    VALID = "valid"
    #: CREATE_PERMANENT_SUPPLIER -- a separate controlled capability produces the partner later.
    PENDING_PERMANENT_SUPPLIER = "pending_permanent_supplier"
    #: USE_ONE_OFF_SUPPLIER -- recorded as intent; execution is deferred pending an
    #: Odoo accounting model that does not pool payables/aging under one partner.
    ONE_OFF_EXECUTION_NOT_SUPPORTED = "one_off_execution_not_supported"


@dataclass(frozen=True, slots=True)
class SupplierResolution(ApplicationDTO):
    """An explicit, immutable supplier-resolution decision for one review version.

    Legal supplier identity (name, VAT) is **not** carried here -- it lives in the
    immutable ``ReviewSourceInvoiceEvidence`` for the review and is the only
    authority for identity validation.
    """

    mode: SupplierResolutionMode
    review_id: str
    company_id: int
    review_version: int
    source_invoice_id: str
    resolved_partner_id: int | None = None
    approved_by: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, SupplierResolutionMode):
            raise SupplierResolutionContractError("A canonical SupplierResolutionMode is required.")
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.review_version, "review_version must be positive.")
        _require_text(self.source_invoice_id, "source_invoice_id is required.")
        if self.mode is SupplierResolutionMode.MATCH_EXISTING:
            _require_positive_int(
                self.resolved_partner_id,
                "MATCH_EXISTING requires a positive resolved_partner_id.",
            )
        elif self.resolved_partner_id is not None:
            raise SupplierResolutionContractError(
                "resolved_partner_id is only valid for MATCH_EXISTING; the legal entity never yields a "
                "permanent partner id for one-off or create-permanent resolutions."
            )
        if self.approved_by is not None and (not isinstance(self.approved_by, str) or not self.approved_by.strip()):
            raise SupplierResolutionContractError("approved_by must be a non-empty name when provided.")
        if self.note is not None:
            if not isinstance(self.note, str) or not self.note.strip():
                raise SupplierResolutionContractError("note must be non-empty text when provided.")
            if len(self.note) > SUPPLIER_RESOLUTION_NOTE_MAX_LENGTH:
                raise SupplierResolutionContractError("note is too long.")


@dataclass(frozen=True, slots=True)
class ResolutionPartnerRecord(ApplicationDTO):
    """Minimal read-only projection of an Odoo ``res.partner`` for MATCH_EXISTING validation."""

    id: int
    name: str | None
    vat: str | None
    active: bool
    company_id: int | None


@dataclass(frozen=True, slots=True)
class SupplierResolutionValidation(ApplicationDTO):
    """Result of :class:`ValidateSupplierResolutionUseCase`."""

    status: SupplierResolutionValidationStatus
    mode: SupplierResolutionMode
    review_id: str
    company_id: int
    source_supplier_tax_number: str | None = None
    effective_partner_id: int | None = None
    safe_message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, SupplierResolutionValidationStatus):
            raise SupplierResolutionContractError("A canonical SupplierResolutionValidationStatus is required.")
        if not isinstance(self.mode, SupplierResolutionMode):
            raise SupplierResolutionContractError("A canonical SupplierResolutionMode is required.")
        if self.effective_partner_id is not None:
            _require_positive_int(self.effective_partner_id, "effective_partner_id must be positive.")
        if (self.status is SupplierResolutionValidationStatus.VALID) != (self.effective_partner_id is not None):
            raise SupplierResolutionContractError("effective_partner_id is set exactly when the resolution is VALID.")


def normalize_supplier_vat(value: str | None) -> str | None:
    """Whitespace-strip only -- identical to ``PartnerMatchingEngine._clean``.

    No prefix removal and no digit extraction, so a selected partner's VAT is
    compared to the immutable source invoice supplier VAT on exactly the same
    terms the deterministic read-only matcher uses.
    """

    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise SupplierResolutionContractError(message)


def _require_positive_int(value: int | None, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise SupplierResolutionContractError(message)
