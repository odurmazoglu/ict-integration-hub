from __future__ import annotations

from typing import Protocol

from app.application.commands.supplier_partner import CreateSupplierPartnerCommand
from app.application.dto.supplier_partner import SupplierPartnerWriteResult


class SupplierPartnerWriter(Protocol):
    """Port for the narrow, protected capability of creating one Odoo supplier partner.

    The application layer depends only on this port -- never on the Odoo JSON-2
    client, JSON-RPC payloads, or ``res.partner`` implementation details.
    """

    async def create_supplier(self, command: CreateSupplierPartnerCommand) -> SupplierPartnerWriteResult:
        pass
