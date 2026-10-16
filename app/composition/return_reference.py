"""Composition for the operator-triggered return-invoice reference lookup (PR R).

Adapters translate connector / ERP / storage failures into application errors so the use
case depends on ports only. Everything here is read-only: Hub reads go through existing
readers, Uyumsoft through the allowlisted ``GetOutboxInvoiceList``, Odoo through the
read-only adapter.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Protocol

from sqlalchemy.orm import Session

from app.application.return_reference import (
    LookupReturnReferenceUseCase,
    OutboxInvoiceCandidate,
    OutboxLookupPage,
    OutboxLookupUnavailableError,
    ReturnReferenceLookupUnavailableError,
    ReturnReferenceSourceDocumentError,
)
from app.connectors.exceptions import ConnectorError
from app.connectors.odoo.client import OdooJson2Client
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.connectors.uyumsoft.invoice_mapping import UYUMSOFT_LOCAL_TIMEZONE
from app.core.config import Settings
from app.erp.exceptions import ErpRepositoryError
from app.erp.models import Company
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.company_repository import OdooCompanyRepository
from app.persistence import SqlAlchemyReviewRepository, SqlAlchemyReviewSourceInvoiceEvidenceReader
from app.persistence.workbench_review_source_invoice_correction_repository import (
    SqlAlchemyReviewSourceInvoiceCorrectionRepository,
)
from app.schemas.uyumsoft_invoices import (
    UyumsoftInvoiceListRequest,
    UyumsoftOutboxInvoiceRecord,
    UyumsoftOutboxLookupResponse,
)
from app.services.document_storage import DocumentStorage, DocumentStorageError, LocalDocumentStorage

#: Small on purpose: an exact-number search should return 0 or 1 rows; anything beyond the
#: page is reported as PROVIDER_RESULT_TRUNCATED (AMBIGUOUS), or as PROVIDER_TOTAL_MISSING
#: (INCOMPLETE_SEARCH) when a full page carries no total -- never paged through.
OUTBOX_LOOKUP_PAGE_SIZE = 10
#: Search window around the reference IssueDate, in Turkish local calendar days.
OUTBOX_LOOKUP_WINDOW_DAYS = 1


class _OutboxLookupClient(Protocol):
    def lookup_outbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftOutboxLookupResponse:
        pass


class _CompanyRepository(Protocol):
    def find_by_tax_number(self, tax_number: str) -> tuple[Company, ...]:
        pass


class UyumsoftOutboxInvoiceLookup:
    def __init__(self, client: _OutboxLookupClient, *, page_size: int = OUTBOX_LOOKUP_PAGE_SIZE) -> None:
        self._client = client
        self._page_size = page_size

    def find_by_invoice_number(self, invoice_number: str, *, issue_date: date, archived: bool) -> OutboxLookupPage:
        request = UyumsoftInvoiceListRequest(
            from_date=datetime.combine(
                issue_date - timedelta(days=OUTBOX_LOOKUP_WINDOW_DAYS), time.min, tzinfo=UYUMSOFT_LOCAL_TIMEZONE
            ),
            to_date=datetime.combine(
                issue_date + timedelta(days=OUTBOX_LOOKUP_WINDOW_DAYS), time.max, tzinfo=UYUMSOFT_LOCAL_TIMEZONE
            ),
            page=1,
            page_size=self._page_size,
            invoice_numbers=(invoice_number,),
            is_archived=archived,
        )
        try:
            response = self._client.lookup_outbox_invoices(request)
        except ConnectorError as exc:
            raise OutboxLookupUnavailableError(exc.safe_message) from exc
        return OutboxLookupPage(
            candidates=tuple(_candidate(record) for record in response.invoices),
            provider_total=response.total_count,
            page_size=self._page_size,
        )


class OdooCompanyTaxIdentityResolver:
    def __init__(self, repository: _CompanyRepository) -> None:
        self._repository = repository

    def company_ids_for_tax_number(self, tax_number: str) -> tuple[int, ...]:
        try:
            companies = self._repository.find_by_tax_number(tax_number)
        except ErpRepositoryError as exc:
            raise ReturnReferenceLookupUnavailableError("Company identity could not be verified.") from exc
        return tuple(company.id for company in companies)


class StoredSourceDocumentReader:
    def __init__(self, storage: DocumentStorage) -> None:
        self._storage = storage

    def read(self, storage_key: str) -> bytes:
        try:
            return self._storage.read(storage_key)
        except DocumentStorageError as exc:
            raise ReturnReferenceSourceDocumentError("Stored source document could not be read.") from exc


def build_lookup_return_reference_use_case(
    *,
    session: Session,
    settings: Settings,
    uyumsoft_client: _OutboxLookupClient | None = None,
    odoo_client: OdooJson2Client | None = None,
    storage: DocumentStorage | None = None,
) -> LookupReturnReferenceUseCase:
    review_repository = SqlAlchemyReviewRepository(session)
    adapter = OdooReadOnlyAdapter(client=odoo_client or OdooJson2Client.from_settings(settings))
    return LookupReturnReferenceUseCase(
        review_reader=review_repository,
        source_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        document_locator=SqlAlchemyReviewSourceInvoiceCorrectionRepository(session),
        document_reader=StoredSourceDocumentReader(storage or LocalDocumentStorage(settings.document_storage_root)),
        company_resolver=OdooCompanyTaxIdentityResolver(OdooCompanyRepository(adapter=adapter)),
        outbox_lookup=UyumsoftOutboxInvoiceLookup(uyumsoft_client or UyumsoftSoapClient.from_settings(settings)),
        outbox_account_company_id=settings.uyumsoft_account_company_id,
    )


def _candidate(record: UyumsoftOutboxInvoiceRecord) -> OutboxInvoiceCandidate:
    return OutboxInvoiceCandidate(
        invoice_number=record.invoice_number,
        ettn=record.ettn,
        invoice_type=record.invoice_type,
        scenario=record.scenario,
        status=record.status,
        buyer_tax_number=record.target_tax_number,
        currency=record.currency,
        issue_date=record.issue_date,
        archived=record.is_archived,
    )


__all__ = [
    "OdooCompanyTaxIdentityResolver",
    "StoredSourceDocumentReader",
    "UyumsoftOutboxInvoiceLookup",
    "build_lookup_return_reference_use_case",
]
