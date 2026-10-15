"""Operator-triggered return-invoice reference lookup (PR R).

Read-only and informational: the use case reads the review, its source-invoice evidence
and the stored raw UBL document, extracts the BillingReference entries at read time,
searches the company's Uyumsoft outbox (active, then archived) by exact invoice number
and validates the single candidate. It has no writer dependency, persists nothing and
is never invoked by a Workbench read -- only by the explicit lookup action.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from app.application.return_reference.exceptions import (
    OutboxLookupUnavailableError,
    ReturnReferenceNotApplicableError,
    ReturnReferenceSourceDocumentError,
)
from app.application.return_reference.matching import (
    deduplicate,
    evaluate_candidate,
    exact_candidates,
    overall_status,
    reference_format_issues,
    reference_status,
)
from app.application.return_reference.model import (
    CandidateEvaluation,
    OutboxInvoiceCandidate,
    OutboxLookupPage,
    ReferenceResult,
    ReturnReferenceCheck,
    ReturnReferenceIssue,
    ReturnReferenceLookupResult,
    ReturnReferenceStatus,
    SearchOutcome,
)
from app.application.workbench.dto import ReviewItem
from app.application.workbench.evidence import ReviewSourceInvoiceEvidence
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workbench.queries import ReviewDetailQuery
from app.domain.invoice import InternalInvoice
from app.domain.invoice.billing_reference import BillingReference, extract_billing_references
from app.domain.invoice.document_types import return_invoice_type_code
from app.domain.invoice.exceptions import InvoiceDomainError

logger = logging.getLogger(__name__)


class ReviewReader(Protocol):
    def get_review_item(self, query: ReviewDetailQuery) -> ReviewItem:
        pass


class SourceInvoiceReader(Protocol):
    def get(self, *, review_id: str, company_id: int) -> ReviewSourceInvoiceEvidence:
        pass


class SourceDocument(Protocol):
    storage_key: str
    content_sha256: str


class SourceDocumentLocator(Protocol):
    def find_source_documents(self, *, review_id: str, company_id: int) -> tuple[SourceDocument, ...]:
        pass


class SourceDocumentContentReader(Protocol):
    """Raises :class:`ReturnReferenceSourceDocumentError` when the document cannot be read."""

    def read(self, storage_key: str) -> bytes:
        pass


class CompanyTaxIdentityResolver(Protocol):
    """Raises :class:`ReturnReferenceLookupUnavailableError` when the ERP cannot be read."""

    def company_ids_for_tax_number(self, tax_number: str) -> tuple[int, ...]:
        pass


class OutboxInvoiceLookup(Protocol):
    """Exact-number search of one outbox partition; raises :class:`OutboxLookupUnavailableError`."""

    def find_by_invoice_number(self, invoice_number: str, *, issue_date: date, archived: bool) -> OutboxLookupPage:
        pass


@dataclass(frozen=True, slots=True)
class ReturnReferenceLookupQuery:
    review_id: str
    company_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.review_id, str) or not self.review_id.strip():
            raise WorkbenchContractError("review_id is required.")
        if type(self.company_id) is not int or self.company_id <= 0:
            raise WorkbenchContractError("company_id must be positive.")


@dataclass(frozen=True, slots=True)
class _SearchResult:
    searches: tuple[SearchOutcome, ...]
    candidates: tuple[OutboxInvoiceCandidate, ...]


class LookupReturnReferenceUseCase:
    def __init__(
        self,
        *,
        review_reader: ReviewReader,
        source_reader: SourceInvoiceReader,
        document_locator: SourceDocumentLocator,
        document_reader: SourceDocumentContentReader,
        company_resolver: CompanyTaxIdentityResolver,
        outbox_lookup: OutboxInvoiceLookup,
    ) -> None:
        self._review_reader = review_reader
        self._source_reader = source_reader
        self._document_locator = document_locator
        self._document_reader = document_reader
        self._company_resolver = company_resolver
        self._outbox_lookup = outbox_lookup

    def execute(self, query: ReturnReferenceLookupQuery) -> ReturnReferenceLookupResult:
        if not isinstance(query, ReturnReferenceLookupQuery):
            raise WorkbenchContractError("ReturnReferenceLookupQuery is required.")
        review = self._review_reader.get_review_item(
            ReviewDetailQuery(review_id=query.review_id, company_id=query.company_id)
        )
        source = self._source_reader.get(review_id=query.review_id, company_id=query.company_id)
        return_code = return_invoice_type_code(source.invoice)
        if return_code is None:
            raise ReturnReferenceNotApplicableError("Review source invoice is not a return invoice.")
        result = _Lookup(
            query=query,
            review_version=review.version,
            return_code=return_code,
            invoice=source.invoice,
            document_locator=self._document_locator,
            document_reader=self._document_reader,
            company_resolver=self._company_resolver,
            outbox_lookup=self._outbox_lookup,
        ).run()
        logger.info(
            "return_reference_lookup_completed",
            extra={
                "review_id": result.review_id,
                "company_id": result.company_id,
                "status": result.status.value,
                "reference_count": len(result.references),
                "reference_statuses": [reference.status.value for reference in result.references],
                "provider_calls": result.provider_calls,
                "issues": [issue.value for issue in result.issues],
            },
        )
        return result


class _Lookup:
    """One execution's state (provider call count, per-number cache)."""

    def __init__(
        self,
        *,
        query: ReturnReferenceLookupQuery,
        review_version: int,
        return_code: str,
        invoice: InternalInvoice,
        document_locator: SourceDocumentLocator,
        document_reader: SourceDocumentContentReader,
        company_resolver: CompanyTaxIdentityResolver,
        outbox_lookup: OutboxInvoiceLookup,
    ) -> None:
        self._query = query
        self._review_version = review_version
        self._return_code = return_code
        self._invoice = invoice
        self._document_locator = document_locator
        self._document_reader = document_reader
        self._company_resolver = company_resolver
        self._outbox_lookup = outbox_lookup
        self._provider_calls = 0
        self._cache: dict[tuple[str, date], _SearchResult] = {}

    def run(self) -> ReturnReferenceLookupResult:
        content_or_issue = self._source_content()
        if isinstance(content_or_issue, ReturnReferenceIssue):
            return self._result(ReturnReferenceStatus.REFERENCE_UNPARSEABLE, issues=(content_or_issue,))
        try:
            extraction = extract_billing_references(content_or_issue)
        except InvoiceDomainError:
            return self._result(ReturnReferenceStatus.REFERENCE_UNPARSEABLE, issues=(ReturnReferenceIssue.XML_INVALID,))
        if not _same_identifier(extraction.invoice_uuid, self._invoice.header.invoice_uuid):
            return self._result(
                ReturnReferenceStatus.REFERENCE_UNPARSEABLE,
                issues=(ReturnReferenceIssue.SOURCE_DOCUMENT_IDENTITY_MISMATCH,),
            )
        if not extraction.references:
            return self._result(ReturnReferenceStatus.REFERENCE_MISSING)

        searchable = any(not reference_format_issues(reference) for reference in extraction.references)
        company_verified = self._company_identity_verified() if searchable else None
        references = tuple(
            self._reference_result(reference, company_verified=bool(company_verified))
            for reference in extraction.references
        )
        return self._result(
            overall_status(reference.status for reference in references),
            references=references,
            company_identity_verified=company_verified,
        )

    def _source_content(self) -> bytes | ReturnReferenceIssue:
        documents = self._document_locator.find_source_documents(
            review_id=self._query.review_id, company_id=self._query.company_id
        )
        if not documents:
            return ReturnReferenceIssue.SOURCE_DOCUMENT_MISSING
        if len(documents) > 1:
            return ReturnReferenceIssue.SOURCE_DOCUMENT_AMBIGUOUS
        document = documents[0]
        try:
            content = self._document_reader.read(document.storage_key)
        except ReturnReferenceSourceDocumentError:
            logger.warning(
                "return_reference_source_document_unreadable",
                extra={"review_id": self._query.review_id, "company_id": self._query.company_id},
            )
            return ReturnReferenceIssue.SOURCE_DOCUMENT_UNREADABLE
        if hashlib.sha256(content).hexdigest() != document.content_sha256:
            return ReturnReferenceIssue.SOURCE_DOCUMENT_HASH_MISMATCH
        return content

    def _company_identity_verified(self) -> bool:
        tax_number = (self._invoice.customer.tax_number or "").strip()
        if not tax_number:
            return False
        return self._company_resolver.company_ids_for_tax_number(tax_number) == (self._query.company_id,)

    def _reference_result(self, reference: BillingReference, *, company_verified: bool) -> ReferenceResult:
        format_issues = reference_format_issues(reference)
        if format_issues:
            return _reference(reference, ReturnReferenceStatus.REFERENCE_UNPARSEABLE, issues=format_issues)
        if reference.invoice_number is None or reference.issue_date is None:  # excluded by format_issues
            return _reference(reference, ReturnReferenceStatus.REFERENCE_UNPARSEABLE)
        search = self._search(reference.invoice_number, reference.issue_date)
        evaluations = tuple(
            evaluate_candidate(
                candidate,
                reference_issue_date=reference.issue_date,
                return_issue_date=self._invoice.header.issue_date,
                return_type_code=self._return_code,
                return_seller_tax_number=self._invoice.supplier.tax_number,
                return_currency=self._invoice.header.currency_code,
                company_identity_verified=company_verified,
            )
            for candidate in search.candidates
        )
        status, issues = reference_status(searches=search.searches, evaluations=evaluations)
        return _reference(
            reference,
            status,
            issues=issues,
            failed_checks=evaluations[0].failed_checks if status is ReturnReferenceStatus.MISMATCH else (),
            searches=search.searches,
            candidates=evaluations,
        )

    def _search(self, invoice_number: str, issue_date: date) -> _SearchResult:
        key = (invoice_number, issue_date)
        if key not in self._cache:
            outcomes: list[SearchOutcome] = []
            candidates: list[OutboxInvoiceCandidate] = []
            for archived in (False, True):
                outcome, exact = self._search_partition(invoice_number, issue_date, archived=archived)
                outcomes.append(outcome)
                candidates.extend(exact)
            self._cache[key] = _SearchResult(searches=tuple(outcomes), candidates=deduplicate(candidates))
        return self._cache[key]

    def _search_partition(
        self, invoice_number: str, issue_date: date, *, archived: bool
    ) -> tuple[SearchOutcome, tuple[OutboxInvoiceCandidate, ...]]:
        self._provider_calls += 1
        try:
            page = self._outbox_lookup.find_by_invoice_number(invoice_number, issue_date=issue_date, archived=archived)
        except OutboxLookupUnavailableError as exc:
            logger.warning(
                "return_reference_outbox_search_failed",
                extra={
                    "review_id": self._query.review_id,
                    "company_id": self._query.company_id,
                    "archived": archived,
                    "safe_message": exc.safe_message,
                },
            )
            return SearchOutcome(archived=archived, completed=False), ()
        exact = exact_candidates(invoice_number, page.candidates)
        return (
            SearchOutcome(
                archived=archived,
                completed=True,
                provider_total=page.provider_total,
                returned=len(page.candidates),
                exact=len(exact),
            ),
            exact,
        )

    def _result(
        self,
        status: ReturnReferenceStatus,
        *,
        issues: tuple[ReturnReferenceIssue, ...] = (),
        references: tuple[ReferenceResult, ...] = (),
        company_identity_verified: bool | None = None,
    ) -> ReturnReferenceLookupResult:
        return ReturnReferenceLookupResult(
            review_id=self._query.review_id,
            company_id=self._query.company_id,
            review_version=self._review_version,
            return_invoice_type_code=self._return_code,
            status=status,
            issues=issues,
            references=references,
            provider_calls=self._provider_calls,
            company_identity_verified=company_identity_verified,
        )


def _reference(
    reference: BillingReference,
    status: ReturnReferenceStatus,
    *,
    issues: tuple[ReturnReferenceIssue, ...] = (),
    failed_checks: tuple[ReturnReferenceCheck, ...] = (),
    searches: tuple[SearchOutcome, ...] = (),
    candidates: tuple[CandidateEvaluation, ...] = (),
) -> ReferenceResult:
    return ReferenceResult(
        position=reference.position,
        invoice_number=reference.invoice_number,
        issue_date=reference.issue_date,
        document_type_code=reference.document_type_code,
        status=status,
        issues=issues,
        failed_checks=failed_checks,
        searches=searches,
        candidates=candidates,
    )


def _same_identifier(left: str | None, right: str | None) -> bool:
    return left is not None and right is not None and left.strip().lower() == right.strip().lower()
