from __future__ import annotations

from typing import Protocol

from app.application.commands.product_remediation import CreateSupplierInfoCommand
from app.application.dto.product_remediation import SupplierInfoWriteResult


class SupplierInfoWriter(Protocol):
    """Port for the narrow, protected capability of creating one Odoo ``product.supplierinfo``.

    The application layer depends only on this port -- never on the Odoo JSON-2
    client, JSON-RPC payloads, or ``product.supplierinfo`` implementation details.
    """

    async def create_supplier_info(self, command: CreateSupplierInfoCommand) -> SupplierInfoWriteResult:
        pass
