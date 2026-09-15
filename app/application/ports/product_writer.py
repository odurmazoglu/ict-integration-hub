from __future__ import annotations

from typing import Protocol

from app.application.commands.product_remediation import CreateProductCommand
from app.application.dto.product_remediation import ProductWriteResult


class ProductWriter(Protocol):
    """Port for the narrow, protected capability of creating one Odoo ``product.template``.

    The application layer depends only on this port -- never on the Odoo JSON-2
    client, JSON-RPC payloads, or ``product.template`` implementation details.
    """

    async def create_product(self, command: CreateProductCommand) -> ProductWriteResult:
        pass
