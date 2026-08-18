from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.application.dto import ExistingInvoiceImport
from app.models.workbench_review_item import WorkbenchReviewItem


class SqlAlchemyImportHistory:
    """Duplicate import reader backed by Hub Workbench review idempotency evidence."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_imported_invoice(self, idempotency_key: str) -> ExistingInvoiceImport | None:
        key = idempotency_key.strip()
        if not key:
            return None
        record = self._session.scalar(
            select(WorkbenchReviewItem).where(WorkbenchReviewItem.idempotency_key == key).limit(1)
        )
        if record is None:
            return None
        return ExistingInvoiceImport(invoice_id=record.invoice_id, vendor_bill_id=None, status="already_imported")
