from __future__ import annotations

from dataclasses import dataclass, field

from app.application.commands.base import Command
from app.domain.invoice import InternalInvoice


@dataclass(frozen=True, slots=True)
class ImportSessionCommand(Command):
    """Application request for importing multiple invoices sequentially."""

    invoices: tuple[InternalInvoice, ...] = field(default_factory=tuple)
    session_id: str | None = None
    company_id: int | None = None
    dry_run: bool = True
    approved_by: str | None = None
