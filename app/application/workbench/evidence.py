from __future__ import annotations

from dataclasses import dataclass

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import WorkbenchContractError
from app.domain.invoice import InternalInvoice
from app.matching import InvoiceProductMatchResult, PartnerMatchResult
from app.tax_mapping import InvoiceTaxMappingResult


@dataclass(frozen=True, slots=True)
class ReviewExecutionEvidence(ApplicationDTO):
    """Immutable pre-decision evidence pinned to a Workbench review version."""

    review_id: str
    company_id: int
    review_version: int
    source_invoice_id: str
    invoice: InternalInvoice
    partner_match: PartnerMatchResult
    product_match: InvoiceProductMatchResult
    tax_match: InvoiceTaxMappingResult

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.review_version, "review_version must be positive.")
        _require_text(self.source_invoice_id, "source_invoice_id is required.")
        if not isinstance(self.invoice, InternalInvoice):
            raise WorkbenchContractError("InternalInvoice DTO is required.")
        if not isinstance(self.partner_match, PartnerMatchResult):
            raise WorkbenchContractError("PartnerMatchResult DTO is required.")
        if not isinstance(self.product_match, InvoiceProductMatchResult):
            raise WorkbenchContractError("InvoiceProductMatchResult DTO is required.")
        if not isinstance(self.tax_match, InvoiceTaxMappingResult):
            raise WorkbenchContractError("InvoiceTaxMappingResult DTO is required.")
        invoice_identity = self.invoice.header.ettn or self.invoice.header.invoice_uuid
        if self.source_invoice_id != invoice_identity:
            raise WorkbenchContractError("source_invoice_id must match InternalInvoice identity.")
        _validate_product_scope(self.invoice, self.product_match)
        _validate_tax_scope(self.invoice, self.tax_match)


def _validate_product_scope(invoice: InternalInvoice, product_match: InvoiceProductMatchResult) -> None:
    invoice_line_numbers = tuple(line.line_number for line in invoice.lines)
    result_line_numbers = tuple(result.line_number for result in product_match.line_results)
    if result_line_numbers != invoice_line_numbers:
        raise WorkbenchContractError("Product match evidence must cover every invoice line in order.")


def _validate_tax_scope(invoice: InternalInvoice, tax_match: InvoiceTaxMappingResult) -> None:
    invoice_tax_keys = tuple(
        (line.line_number, tax_index) for line in invoice.lines for tax_index, _tax in enumerate(line.taxes)
    )
    result_tax_keys = tuple((result.line_number, result.tax_index) for result in tax_match.line_results)
    if result_tax_keys != invoice_tax_keys:
        raise WorkbenchContractError("Tax mapping evidence must cover every invoice tax in order.")


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(message)


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)
