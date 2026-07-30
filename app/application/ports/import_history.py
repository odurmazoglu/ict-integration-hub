from __future__ import annotations

from typing import Protocol

from app.application.dto import ExistingInvoiceImport


class InvoiceImportHistory(Protocol):
    """Port for duplicate detection before a Vendor Bill import executes."""

    def find_imported_invoice(self, idempotency_key: str) -> ExistingInvoiceImport | None:
        pass
