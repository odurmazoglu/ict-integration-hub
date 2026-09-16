from __future__ import annotations

from dataclasses import dataclass

from app.application.commands.base import Command
from app.application.exceptions.supplier_partner import SupplierPartnerWriteValidationError


@dataclass(frozen=True, slots=True)
class ArchiveOneOffVendorPartnerCommand(Command):
    """Application request to retire exactly one Odoo ``res.partner`` (active=False).

    Carries only the partner id and the authenticated approver -- never any other
    field. The narrowest possible write: no name/vat/company, no delete capability.
    """

    partner_id: int
    approved_by: str | None = None

    def __post_init__(self) -> None:
        if type(self.partner_id) is not int or isinstance(self.partner_id, bool) or self.partner_id <= 0:
            raise SupplierPartnerWriteValidationError("A positive partner_id is required.")
        if self.approved_by is not None and (not isinstance(self.approved_by, str) or not self.approved_by.strip()):
            raise SupplierPartnerWriteValidationError("approved_by must be a non-empty name when provided.")
