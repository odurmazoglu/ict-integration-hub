from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.application.dto.base import ApplicationDTO

ImportInvoiceStatus = Literal["dry_run", "created", "already_imported", "already_exists", "failed"]


@dataclass(frozen=True, slots=True)
class ExistingInvoiceImport(ApplicationDTO):
    """Existing import record returned by an import-history port."""

    invoice_id: str
    vendor_bill_id: int | None = None
    status: ImportInvoiceStatus = "already_imported"


@dataclass(frozen=True, slots=True)
class ImportInvoiceResult(ApplicationDTO):
    """Use-case result for a deterministic single-invoice Vendor Bill import."""

    success: bool
    invoice_id: str
    status: ImportInvoiceStatus
    vendor_bill_id: int | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
    duration: float = 0.0
