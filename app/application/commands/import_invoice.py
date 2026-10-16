from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.application.commands.base import Command
from app.domain.invoice import InternalInvoice

if TYPE_CHECKING:
    from app.application.effective_supplier import AcceptedSupplier
    from app.application.source_tax.model import SourceTaxAssessment


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
    #: PR T W1: the verified tax facts of the invoice's original UBL document (or why it
    #: could not be verified). Set by canonical import from the bytes it just stored and by
    #: ``EffectiveDecisionResolver`` from the stored document; never part of source evidence.
    #: ``None`` is only tolerated for a dry run -- a real write without it fails closed.
    source_tax_assessment: SourceTaxAssessment | None = None
