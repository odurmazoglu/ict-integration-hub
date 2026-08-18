from __future__ import annotations

from typing import Protocol

from app.application.dto import ExistingInvoiceImport
from app.application.dto.import_invoice import ImportInvoiceResult


class InvoiceImportHistory(Protocol):
    """Port for duplicate detection before a Vendor Bill import executes."""

    def find_imported_invoice(self, idempotency_key: str) -> ExistingInvoiceImport | None:
        pass

    def record_import_result(
        self,
        *,
        company_id: int,
        idempotency_key: str,
        result: ImportInvoiceResult,
    ) -> ExistingInvoiceImport:
        pass
