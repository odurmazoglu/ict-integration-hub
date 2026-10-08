from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.application.commands.base import Command
from app.domain.invoice import InternalInvoice

if TYPE_CHECKING:
    from app.application.effective_supplier import AcceptedSupplier


@dataclass(frozen=True, slots=True)
class ImportInvoiceCommand(Command):
    """Application request for importing one invoice through the Vendor Bill path."""

    invoice: InternalInvoice
    idempotency_key: str
    company_id: int | None = None
    dry_run: bool = True
    approved_by: str | None = None
    #: The review's proven accepted supplier resolution, when reclassifying an existing
    #: review. Only product matching consults it, and only when the raw deterministic
    #: partner match is not MATCHED (``resolve_effective_supplier``). Never set on import.
    accepted_supplier: AcceptedSupplier | None = None
