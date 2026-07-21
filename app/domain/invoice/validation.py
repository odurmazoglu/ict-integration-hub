from __future__ import annotations

from dataclasses import dataclass

from app.domain.invoice.dto import InternalInvoice, Party


@dataclass(frozen=True, slots=True)
class InvoiceValidationIssue:
    field: str
    message: str


def validate_invoice(invoice: InternalInvoice) -> tuple[InvoiceValidationIssue, ...]:
    issues: list[InvoiceValidationIssue] = []
    if not invoice.header.invoice_number.strip():
        issues.append(InvoiceValidationIssue("header.invoice_number", "Invoice number is required."))
    if not invoice.header.invoice_uuid.strip():
        issues.append(InvoiceValidationIssue("header.invoice_uuid", "Invoice UUID is required."))
    if not _party_present(invoice.supplier):
        issues.append(InvoiceValidationIssue("supplier", "Supplier is required."))
    if not _party_present(invoice.customer):
        issues.append(InvoiceValidationIssue("customer", "Customer is required."))
    if not invoice.lines:
        issues.append(InvoiceValidationIssue("lines", "At least one invoice line is required."))
    if not invoice.header.currency_code:
        issues.append(InvoiceValidationIssue("header.currency_code", "Currency is required."))
    return tuple(issues)


def _party_present(party: Party) -> bool:
    return bool(party.name or party.tax_number or party.addresses or party.emails or party.phones)
