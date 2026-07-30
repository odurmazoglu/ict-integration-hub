from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.application.dto.base import ApplicationDTO

VendorBillWriteStatus = Literal["dry_run", "created", "existing", "failed"]


@dataclass(frozen=True, slots=True)
class VendorBillWriteResult(ApplicationDTO):
    """Result returned by a future vendor bill writer port."""

    status: VendorBillWriteStatus
    idempotency_key: str
    external_id: int | None = None
    external_model: str | None = None
    safe_message: str | None = None
    success: bool | None = None
    vendor_bill_id: int | None = None
    draft_number: str | None = None
    already_exists: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
