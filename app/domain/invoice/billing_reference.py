"""UBL-TR ``BillingReference`` extraction for return invoices (PR R).

A GİB return invoice (``InvoiceTypeCode`` IADE family) names the original invoice it
reverses in ``Invoice/cac:BillingReference/cac:InvoiceDocumentReference``: ``cbc:ID`` is
the original's official GİB invoice number and ``cbc:IssueDate`` its issue date.

Extraction runs at read time against the stored raw UBL document. It deliberately does
not extend :class:`~app.domain.invoice.dto.InternalInvoice`: persisted source-invoice
evidence is fingerprinted on replay, so adding a field there would turn every replay of
an invoice carrying a BillingReference into an idempotency conflict.

Values are trimmed and otherwise kept verbatim. Nothing is inferred from ``cbc:Note``,
line descriptions or ``AdditionalDocumentReference`` -- free text is never a reference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from xml.etree import ElementTree

from app.domain.invoice.parser import NS, parse_ubl_invoice_root

#: GİB e-invoice number: 3-character series prefix, 4-digit year, 9-digit sequence.
GIB_INVOICE_NUMBER_PATTERN = re.compile(r"^[A-Z0-9]{3}[0-9]{4}[0-9]{9}$")


@dataclass(frozen=True, slots=True)
class BillingReference:
    """One ``cac:BillingReference/cac:InvoiceDocumentReference`` entry, in document order."""

    position: int
    invoice_number: str | None
    issue_date: date | None
    issue_date_present: bool
    document_type_code: str | None

    @property
    def has_gib_invoice_number(self) -> bool:
        return is_gib_invoice_number(self.invoice_number)


@dataclass(frozen=True, slots=True)
class BillingReferenceExtraction:
    invoice_uuid: str | None
    references: tuple[BillingReference, ...]


def is_gib_invoice_number(value: str | None) -> bool:
    return value is not None and GIB_INVOICE_NUMBER_PATTERN.fullmatch(value) is not None


def extract_billing_references(content: bytes | str) -> BillingReferenceExtraction:
    """Every invoice-document BillingReference of a UBL invoice.

    Raises :class:`~app.domain.invoice.exceptions.InvoiceDomainError` subclasses for
    malformed, unsafe or non-invoice XML -- the same rules as the canonical parser.
    """

    root = parse_ubl_invoice_root(content)
    elements = root.findall("cac:BillingReference/cac:InvoiceDocumentReference", NS)
    return BillingReferenceExtraction(
        invoice_uuid=_text(root, "cbc:UUID"),
        references=tuple(_reference(position, element) for position, element in enumerate(elements, start=1)),
    )


def _reference(position: int, element: ElementTree.Element) -> BillingReference:
    issue_date_text = _text(element, "cbc:IssueDate")
    return BillingReference(
        position=position,
        invoice_number=_text(element, "cbc:ID"),
        issue_date=_date(issue_date_text),
        issue_date_present=issue_date_text is not None,
        document_type_code=_text(element, "cbc:DocumentTypeCode"),
    )


def _text(element: ElementTree.Element, path: str) -> str | None:
    child = element.find(path, NS)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
