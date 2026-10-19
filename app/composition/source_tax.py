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
from app.application.source_tax.exemption import DISABLED_VAT_EXEMPTION_POLICY, VatExemptionTaxPolicy
from app.application.source_tax.reader import SourceTaxSafetyCheck, StoredSourceTaxFactsReader
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.tax_repository import OdooExemptionTaxReader
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


def build_vat_exemption_tax_policy(
    *,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> VatExemptionTaxPolicy:
    """PR T T2-A. Empty ``VAT_EXEMPTION_PURCHASE_TAX_IDS`` composes no Odoo client at all."""

    if not settings.vat_exemption_purchase_tax_ids:
        return DISABLED_VAT_EXEMPTION_POLICY
    client = odoo_client or OdooJson2Client.from_settings(settings)
    return VatExemptionTaxPolicy(
        tax_ids_by_code=settings.vat_exemption_purchase_tax_ids,
        tax_reader=OdooExemptionTaxReader(adapter=OdooReadOnlyAdapter(client=client)),
    )


def build_source_tax_safety_check(
    *,
    session: Session,
    settings: Settings,
    storage: DocumentStorage | None = None,
    odoo_client: OdooJson2Client | None = None,
) -> SourceTaxSafetyCheck:
    return SourceTaxSafetyCheck(
        facts_reader=build_source_tax_facts_reader(session=session, settings=settings, storage=storage),
        exemption_policy=build_vat_exemption_tax_policy(settings=settings, odoo_client=odoo_client),
    )


__all__ = [
    "StoredSourceTaxDocumentReader",
    "build_source_tax_facts_reader",
    "build_source_tax_safety_check",
    "build_vat_exemption_tax_policy",
]
