from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.application.dto.base import ApplicationDTO

CustomerInvoiceWriteStatus = Literal["dry_run", "created", "existing", "failed"]


@dataclass(frozen=True, slots=True)
class CustomerInvoiceWriteResult(ApplicationDTO):
    """Result returned by a draft customer invoice writer port."""

    status: CustomerInvoiceWriteStatus
    idempotency_key: str
    external_id: int | None = None
    external_model: str | None = None
    safe_message: str | None = None
    success: bool | None = None
    customer_invoice_id: int | None = None
    draft_number: str | None = None
    already_exists: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
