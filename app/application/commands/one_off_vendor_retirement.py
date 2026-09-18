from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.application.commands.base import Command
from app.application.exceptions.supplier_partner import SupplierPartnerWriteValidationError

if TYPE_CHECKING:
    # Deferred to avoid a circular import through app.application.workbench's
    # package __init__ (see supplier_partner.py's identical guard for details).
    from app.application.workbench.write_authorization import WriteAuthorizationRecord


@dataclass(frozen=True, slots=True)
class ArchiveOneOffVendorPartnerCommand(Command):
    """Application request to retire exactly one Odoo ``res.partner`` (active=False).

    Carries only the partner id and the authenticated approver -- never any other
    field. The narrowest possible write: no name/vat/company, no delete capability.

    ``authorization`` (P0-PROD-09F): an optional, already-claimed-and-consumed
    ``WriteAuthorizationRecord`` letting ``OdooSupplierPartnerWritePolicy`` bypass
    ONLY ``supplier_remediation_write_enabled`` for this one archive write -- same
    bypass shape and same absolute master-kill-switch/approval/approver checks as
    supplier-partner creation. Never used by the automatic post-execution retirement
    trigger, only the explicit operator recovery workflow.
    """

    partner_id: int
    approved_by: str | None = None
    authorization: WriteAuthorizationRecord | None = None

    def __post_init__(self) -> None:
        if type(self.partner_id) is not int or isinstance(self.partner_id, bool) or self.partner_id <= 0:
            raise SupplierPartnerWriteValidationError("A positive partner_id is required.")
        if self.approved_by is not None and (not isinstance(self.approved_by, str) or not self.approved_by.strip()):
            raise SupplierPartnerWriteValidationError("approved_by must be a non-empty name when provided.")
