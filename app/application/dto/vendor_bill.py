from __future__ import annotations

from dataclasses import dataclass
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
