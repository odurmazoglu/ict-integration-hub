from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
    imported_count: int = 0
    review_count: int = 0
    already_imported_count: int = 0
    failed_import_count: int = 0
    skipped_import_count: int = 0
    import_outcomes: list[dict[str, object]] = Field(default_factory=list)
    status: SyncStatus
    failure_message: str | None = None
    selected_invoices: int = 0


class UyumsoftInvoiceSyncResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: int | None = None
    provider: str
    status: SyncStatus
    created: int
    updated: int
    skipped: int
    imported_count: int = 0
    review_count: int = 0
    already_imported_count: int = 0
    failed_import_count: int = 0
    skipped_import_count: int = 0
    cursor_state: dict[str, object]
    failure_message: str | None = None
    selected_invoices: int = 0
    # Echo of the requested invoice_ettn allowlist (empty when omitted) and which of those
    # exact identities were actually encountered. set(requested) - set(matched) is "not found".
    requested_invoice_ettn: list[str] = Field(default_factory=list)
    matched_invoice_ettn: list[str] = Field(default_factory=list)
    directions: list[DirectionSyncSummaryResponse]
