from __future__ import annotations

from typing import Protocol

from app.application.commands import VendorBillWriteCommand
from app.application.dto import VendorBillWriteResult


class VendorBillWriter(Protocol):
    """Port for infrastructure that can execute an approved vendor bill write."""

    async def write_vendor_bill(self, command: VendorBillWriteCommand) -> VendorBillWriteResult:
        pass
