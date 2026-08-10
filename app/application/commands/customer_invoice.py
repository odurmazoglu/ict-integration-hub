from __future__ import annotations

from dataclasses import dataclass

from app.application.commands.base import Command
from app.billing import CustomerInvoice


@dataclass(frozen=True, slots=True)
class CustomerInvoiceWriteCommand(Command):
    """Application request for an approved draft customer invoice write."""

    customer_invoice: CustomerInvoice
    idempotency_key: str
    dry_run: bool = True
    approved_by: str | None = None
