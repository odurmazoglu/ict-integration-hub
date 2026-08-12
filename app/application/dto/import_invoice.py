from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.application.dto.base import ApplicationDTO
from app.application.workflow import ManualReviewReason

ImportInvoiceStatus = Literal["dry_run", "created", "already_imported", "already_exists", "review_required", "failed"]


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
    review_required: bool = False
    review_reasons: tuple[ManualReviewReason, ...] = field(default_factory=tuple)
    classification_result: object | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
    duration: float = 0.0
