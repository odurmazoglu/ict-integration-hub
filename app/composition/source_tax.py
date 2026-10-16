"""Composition for the PR T W1 source-tax guard.

The review's original UBL document is located with PR R's verified locator (import
idempotency key -> provider metadata -> ``invoice_documents``) and read from the same
document storage import wrote it to. Read-only: nothing here can write.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.application.source_tax import (
    SourceDocumentIssue,
    SourceDocumentUnreadableError,
    SourceTaxFactsUnavailableError,
)
from app.application.source_tax.reader import SourceTaxSafetyCheck, StoredSourceTaxFactsReader
from app.core.config import Settings
from app.persistence.workbench_review_source_invoice_correction_repository import (
    SqlAlchemyReviewSourceInvoiceCorrectionRepository,
)
from app.services.document_storage import (
    DocumentNotFoundError,
    DocumentStorage,
    DocumentStorageError,
    InvalidDocumentStorageKeyError,
    LocalDocumentStorage,
)


class StoredSourceTaxDocumentReader:
    """Separates a verdict about the document from an outage of the storage.

    Only the two deterministic storage conditions -- an unusable key, or a missing file
    under a verified, populated root -- become an unverified-document verdict. Every other
    storage failure (root missing / unmounted / empty, permission denied, I/O) is a
    retryable outage and never persists a blocking reason.
    """

    def __init__(self, storage: DocumentStorage) -> None:
        self._storage = storage

    def read(self, storage_key: str) -> bytes:
        try:
            return self._storage.read(storage_key)
        except InvalidDocumentStorageKeyError as exc:
            raise SourceDocumentUnreadableError(
                "Stored source invoice document key is invalid.", issue=SourceDocumentIssue.STORAGE_KEY_INVALID
            ) from exc
        except DocumentNotFoundError as exc:
            raise SourceDocumentUnreadableError(
                "Stored source invoice document is missing.", issue=SourceDocumentIssue.DOCUMENT_FILE_MISSING
            ) from exc
        except DocumentStorageError as exc:
            raise SourceTaxFactsUnavailableError("Stored source invoice document could not be read.") from exc


def build_source_tax_facts_reader(
    *,
    session: Session,
    settings: Settings,
    storage: DocumentStorage | None = None,
) -> StoredSourceTaxFactsReader:
    return StoredSourceTaxFactsReader(
        document_locator=SqlAlchemyReviewSourceInvoiceCorrectionRepository(session),
        document_reader=StoredSourceTaxDocumentReader(storage or LocalDocumentStorage(settings.document_storage_root)),
    )


def build_source_tax_safety_check(
    *,
    session: Session,
    settings: Settings,
    storage: DocumentStorage | None = None,
) -> SourceTaxSafetyCheck:
    return SourceTaxSafetyCheck(
        facts_reader=build_source_tax_facts_reader(session=session, settings=settings, storage=storage)
    )


__all__ = [
    "StoredSourceTaxDocumentReader",
    "build_source_tax_facts_reader",
    "build_source_tax_safety_check",
]
