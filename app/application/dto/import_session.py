from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from app.application.dto.base import ApplicationDTO
from app.application.dto.import_invoice import ImportInvoiceResult

ImportSessionStatus = Literal["CREATED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"]


@dataclass(frozen=True, slots=True)
class ImportSessionResult(ApplicationDTO):
    """Immutable result for a sequential in-memory import session."""

    session_id: str
    status: ImportSessionStatus
    started_at: datetime
    finished_at: datetime
    duration: float
    processed: int
    successful: int
    duplicates: int
    failed: int
    review_required: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
    results: tuple[ImportInvoiceResult, ...] = field(default_factory=tuple)
