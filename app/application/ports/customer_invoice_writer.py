from __future__ import annotations

from typing import Protocol

from app.application.commands import CustomerInvoiceWriteCommand
from app.application.dto import CustomerInvoiceWriteResult


class CustomerInvoiceWriter(Protocol):
    """Port for infrastructure that can execute an approved customer invoice write."""

    async def write_customer_invoice(self, command: CustomerInvoiceWriteCommand) -> CustomerInvoiceWriteResult:
        pass
