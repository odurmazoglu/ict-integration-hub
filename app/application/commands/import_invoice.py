from __future__ import annotations

from dataclasses import dataclass

from app.application.commands.base import Command
from app.domain.invoice import InternalInvoice


@dataclass(frozen=True, slots=True)
class ImportInvoiceCommand(Command):
    """Application request for importing one invoice through the Vendor Bill path."""

    invoice: InternalInvoice
    idempotency_key: str
    company_id: int | None = None
    dry_run: bool = True
    approved_by: str | None = None
