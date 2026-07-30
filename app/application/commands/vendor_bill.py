from __future__ import annotations

from dataclasses import dataclass

from app.application.commands.base import Command
from app.billing import VendorBill


@dataclass(frozen=True, slots=True)
class VendorBillWriteCommand(Command):
    """Application request for a future vendor bill write use case."""

    vendor_bill: VendorBill
    idempotency_key: str
    dry_run: bool = True
    approved_by: str | None = None
