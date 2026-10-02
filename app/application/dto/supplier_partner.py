from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.application.dto.base import ApplicationDTO
from app.application.exceptions.supplier_partner import SupplierPartnerDataIntegrityError
from app.application.partner_classification import PartnerClassificationOutcome


class SupplierPartnerWriteStatus(StrEnum):
    """Terminal outcome of a controlled supplier partner write request."""

    CREATED = "created"
    ALREADY_EXISTS = "already_exists"


@dataclass(frozen=True, slots=True)
class SupplierPartnerWriteResult(ApplicationDTO):
    """Typed result of a controlled Odoo supplier ``res.partner`` write.

    Carries only sanctioned identity/audit fields -- never a raw Odoo payload.
    ``partner_id`` is always a positive integer (``bool`` is rejected).
    """

    status: SupplierPartnerWriteStatus
    partner_id: int
    company_id: int
    supplier_name: str
    supplier_tax_number: str
    idempotency_key: str
    existing_by: str | None = None
    name_mismatch: bool = False
    safe_message: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    #: How the partner's ICT business classification was handled by this write: set on
    #: create, or evaluated (never changed) for an existing exact-VAT partner.
    classification_outcome: PartnerClassificationOutcome | None = None
    #: The classification value observed on the partner after this write (``None`` if empty).
    classification_value: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, SupplierPartnerWriteStatus):
            raise SupplierPartnerDataIntegrityError("A canonical supplier partner write status is required.")
        if type(self.partner_id) is not int or self.partner_id <= 0:
            raise SupplierPartnerDataIntegrityError("partner_id must be a positive Odoo id.")
        if type(self.company_id) is not int or self.company_id <= 0:
            raise SupplierPartnerDataIntegrityError("company_id must be positive.")
        if self.classification_outcome is not None and not isinstance(
            self.classification_outcome, PartnerClassificationOutcome
        ):
            raise SupplierPartnerDataIntegrityError("A canonical partner classification outcome is required.")
        object.__setattr__(self, "warnings", tuple(self.warnings))
