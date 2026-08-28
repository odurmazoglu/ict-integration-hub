from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.dto import ExistingInvoiceImport, ImportInvoiceResult
from app.application.use_cases.import_invoice import ImportInvoiceInfrastructureError, ImportInvoiceValidationError
from app.models.import_receipt import ImportReceipt
from app.models.workbench_review_item import WorkbenchReviewItem


class SqlAlchemyImportHistory:
    """Canonical import duplicate reader backed by Hub technical receipts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_imported_invoice(self, idempotency_key: str) -> ExistingInvoiceImport | None:
        key = idempotency_key.strip()
        if not key:
            return None
        receipt = self._session.scalar(select(ImportReceipt).where(ImportReceipt.idempotency_key == key).limit(1))
        if receipt is not None:
            return _existing_from_receipt(receipt)
        record = self._session.scalar(
            select(WorkbenchReviewItem).where(WorkbenchReviewItem.idempotency_key == key).limit(1)
        )
        if record is None:
            return None
        return ExistingInvoiceImport(
            invoice_id=record.invoice_id,
            vendor_bill_id=None,
            status="already_imported",
        )

    def record_import_result(
        self,
        *,
        company_id: int,
        idempotency_key: str,
        result: ImportInvoiceResult,
    ) -> ExistingInvoiceImport:
        key = _validate_key(idempotency_key)
        if type(company_id) is not int or company_id <= 0:
            raise ImportInvoiceValidationError("A positive company_id is required for import receipt persistence.")
        existing = self.find_imported_invoice(key)
        if existing is not None:
            return existing
        receipt = ImportReceipt(
            company_id=company_id,
            idempotency_key=key,
            invoice_id=result.invoice_id,
            status=result.status,
            vendor_bill_id=result.vendor_bill_id,
            review_id=result.review_id,
        )
        try:
            with self._session.begin_nested():
                self._session.add(receipt)
                self._session.flush()
        except IntegrityError as exc:
            concurrent = self.find_imported_invoice(key)
            if concurrent is not None:
                return concurrent
            raise ImportInvoiceInfrastructureError("Canonical import receipt persistence failed.") from exc
        except SQLAlchemyError as exc:
            raise ImportInvoiceInfrastructureError("Canonical import receipt persistence failed.") from exc
        return _existing_from_receipt(receipt)


def _validate_key(idempotency_key: str) -> str:
    key = idempotency_key.strip()
    if not key:
        raise ImportInvoiceValidationError("Import idempotency key is required.")
    return key


def _existing_from_receipt(receipt: ImportReceipt) -> ExistingInvoiceImport:
    return ExistingInvoiceImport(
        invoice_id=receipt.invoice_id,
        vendor_bill_id=receipt.vendor_bill_id,
        status="already_imported",
    )
