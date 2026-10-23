"""Foreign-currency Vendor Bill exchange rate (P1): the invoice rate is authoritative.

Shared by EXECUTE and preview. ``VendorBillFxRatePolicy.plan`` applies the rate-selection
rule (:func:`app.billing.fx_rate.select_vendor_bill_rate`) to:

* the verified original UBL document (hash + UUID, located exactly as the PR T W1
  source-tax guard locates it), read fresh on every attempt -- never the fingerprinted
  evidence, which does not carry the rate's currency codes;
* the accepted decision's evidence rate, which must agree with the document;
* read-only Odoo company-currency facts and Odoo's automatic rate (display only).

EXECUTE fails closed on any blocking plan before the writer's first call, then sends the
selected rate on the one new draft bill and verifies Odoo's TRY lines, taxes and payable
with ``verify_created``. ``res.currency.rate`` is never written; no bill is posted.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date
from decimal import Decimal
from typing import Protocol

from app.application.exceptions import ApplicationError
from app.application.source_tax.exceptions import SourceDocumentUnreadableError, SourceTaxFactsUnavailableError
from app.application.source_tax.reader import SourceDocumentContentReader, SourceDocumentLocator
from app.application.workbench.exceptions import ReviewNotFoundError, ReviewPersistenceError
from app.billing.fx_rate import (
    FxRatePlan,
    FxRateStatus,
    FxVerification,
    FxVerificationStatus,
    OdooCurrencyContext,
    VendorBillCompanyAmounts,
    select_vendor_bill_rate,
    verify_vendor_bill_company_amounts,
)
from app.domain.invoice import InternalInvoice
from app.domain.invoice.exceptions import InvoiceDomainError
from app.domain.invoice.fx_facts import SourceFxFacts, extract_source_fx_facts

logger = logging.getLogger(__name__)

#: UBL-TR e-invoices for a TRY company: a TRY bill is never converted, so it is planned
#: without any document or Odoo read and its execution keeps its pre-P1 call sequence.
UNCONVERTED_CURRENCY_CODE = "TRY"


class VendorBillCurrencyContextReader(Protocol):
    """Structurally read-only (``res.company`` / ``res.currency`` search_read). No write method."""

    def resolve_vendor_bill_document_rate(
        self, *, company_id: int, currency_code: str, rate_date: date
    ) -> OdooCurrencyContext:
        pass


class VendorBillCompanyAmountsReader(Protocol):
    """Structurally read-only (``account.move`` / ``account.move.line`` search_read)."""

    def read_vendor_bill_company_amounts(self, *, move_id: int, company_id: int) -> VendorBillCompanyAmounts | None:
        pass


class SourceFxFactsReader(Protocol):
    """The verified exchange-rate facts of a review's original UBL, or None when unverified.
    Raises :class:`SourceFxFactsUnavailableError` (retryable) on a storage outage."""

    def read_source_fx_facts(
        self, *, review_id: str, company_id: int, invoice_uuid: str | None
    ) -> SourceFxFacts | None:
        pass


class SourceFxFactsUnavailableError(ApplicationError):
    error_category = "source_fx_facts_unavailable"


class VendorBillFxRateBlockedError(ApplicationError):
    """A foreign-currency bill whose invoice rate is missing, invalid or contradictory."""

    error_category = "vendor_bill_fx_rate_blocked"

    def __init__(self, plan: FxRatePlan) -> None:
        super().__init__(plan.message)
        self.plan = plan

    @property
    def error_code(self) -> str:
        return f"vendor_bill_fx_{self.plan.status.value}"


class StoredSourceFxFactsReader:
    """Reads the review's original stored UBL and trusts it only when hash and UUID match."""

    def __init__(
        self, *, document_locator: SourceDocumentLocator, document_reader: SourceDocumentContentReader
    ) -> None:
        self._document_locator = document_locator
        self._document_reader = document_reader

    def read_source_fx_facts(
        self, *, review_id: str, company_id: int, invoice_uuid: str | None
    ) -> SourceFxFacts | None:
        try:
            documents = self._document_locator.find_source_documents(review_id=review_id, company_id=company_id)
        except ReviewNotFoundError:
            raise
        except ReviewPersistenceError as exc:
            raise SourceFxFactsUnavailableError("Source invoice document could not be located right now.") from exc
        if len(documents) != 1:
            return None
        document = documents[0]
        try:
            content = self._document_reader.read(document.storage_key)
        except SourceDocumentUnreadableError:
            return None
        except SourceTaxFactsUnavailableError as exc:
            raise SourceFxFactsUnavailableError("Stored source invoice document could not be read.") from exc
        expected_sha256 = (document.content_sha256 or "").strip().lower()
        if not expected_sha256 or hashlib.sha256(content).hexdigest() != expected_sha256:
            return None
        try:
            facts = extract_source_fx_facts(content)
        except InvoiceDomainError:
            return None
        expected_uuid = (invoice_uuid or "").strip()
        if not expected_uuid or facts.invoice_uuid != expected_uuid:
            return None
        return facts


class VendorBillFxRatePolicy:
    def __init__(
        self,
        *,
        facts_reader: SourceFxFactsReader,
        currency_reader: VendorBillCurrencyContextReader,
        amounts_reader: VendorBillCompanyAmountsReader,
    ) -> None:
        self._facts_reader = facts_reader
        self._currency_reader = currency_reader
        self._amounts_reader = amounts_reader

    def plan(
        self,
        *,
        review_id: str,
        company_id: int,
        invoice: InternalInvoice,
        rate_date: date,
        line_subtotals: tuple[Decimal, ...],
        tax_total: Decimal | None,
    ) -> FxRatePlan:
        """Read failures propagate (EXECUTE maps them to retryable failures)."""

        currency = (invoice.header.currency_code or "").strip().upper()
        if currency == UNCONVERTED_CURRENCY_CODE:
            return FxRatePlan(
                status=FxRateStatus.NOT_APPLICABLE,
                document_currency=currency,
                company_currency=UNCONVERTED_CURRENCY_CODE,
                rate_date=rate_date,
                message="Bill is in the company currency.",
            )
        odoo = self._currency_reader.resolve_vendor_bill_document_rate(
            company_id=company_id, currency_code=currency, rate_date=rate_date
        )
        facts = self._facts_reader.read_source_fx_facts(
            review_id=review_id, company_id=company_id, invoice_uuid=invoice.header.invoice_uuid
        )
        plan = select_vendor_bill_rate(
            document_currency=currency,
            rate_date=rate_date,
            evidence_rate=invoice.header.exchange_rate,
            facts=facts,
            odoo=odoo,
            line_subtotals=line_subtotals,
            tax_total=tax_total,
        )
        logger.info(
            "vendor_bill_fx_rate_planned",
            extra={
                "review_id": review_id,
                "company_id": company_id,
                "status": plan.status.value,
                "currency": currency,
                "source_rate": str(plan.source_rate) if plan.source_rate is not None else None,
                "odoo_automatic_rate": str(plan.odoo_automatic_rate) if plan.odoo_automatic_rate is not None else None,
            },
        )
        return plan

    def verify_created(self, plan: FxRatePlan, *, move_id: int, company_id: int) -> FxVerification:
        if not plan.applies_rate:
            return FxVerification(status=FxVerificationStatus.NOT_APPLICABLE)
        actual = self._amounts_reader.read_vendor_bill_company_amounts(move_id=move_id, company_id=company_id)
        return verify_vendor_bill_company_amounts(plan, actual)


def evidence_unavailable_plan(invoice: InternalInvoice, *, rate_date: date, reason: str) -> FxRatePlan:
    """Preview reports a read outage instead of raising."""

    return FxRatePlan(
        status=FxRateStatus.EVIDENCE_UNAVAILABLE,
        document_currency=(invoice.header.currency_code or "").strip().upper(),
        rate_date=rate_date,
        message=f"Exchange-rate evidence could not be read right now: {reason}",
    )
