from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.uyumsoft_invoices import InvoiceDirection

SyncDirection = Literal["Inbox", "Outbox", "Both"]
SyncStatus = Literal["running", "completed", "failed"]


class DirectionSyncSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: InvoiceDirection
    pages_fetched: int
    invoices_seen: int
    created: int
    updated: int
    skipped: int
    status: SyncStatus
    failure_message: str | None = None


class UyumsoftInvoiceSyncResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: int | None = None
    provider: str
    status: SyncStatus
    created: int
    updated: int
    skipped: int
    cursor_state: dict[str, object]
    failure_message: str | None = None
    directions: list[DirectionSyncSummaryResponse]
