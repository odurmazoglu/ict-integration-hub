from __future__ import annotations

from typing import Protocol

from app.application.commands.one_off_vendor_retirement import ArchiveOneOffVendorPartnerCommand
from app.application.dto.one_off_vendor_retirement import OneOffVendorArchiveWriteResult


class OneOffVendorRetirementPort(Protocol):
    """Port for the narrow, protected capability of archiving one Odoo supplier partner.

    Deliberately not a generic ``res.partner`` write port: this is the smallest
    possible capability (archive only, read-back idempotent, no delete) and the
    application layer never depends on anything broader for this purpose.
    """

    async def archive_partner(self, command: ArchiveOneOffVendorPartnerCommand) -> OneOffVendorArchiveWriteResult:
        pass
