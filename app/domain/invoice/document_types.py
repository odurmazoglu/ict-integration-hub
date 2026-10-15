"""UBL-TR document-type semantics that gate Vendor Bill creation.

PR S. ``cbc:InvoiceTypeCode`` is parsed verbatim into ``Header.invoice_type``. A return
invoice (``IADE`` and the GİB return variants below) is not a purchase: it documents goods
or services coming *back*, usually against an invoice ICT itself issued. It must never be
reinterpreted as a supplier purchase. Matching is exact on the trimmed code; there is no
substring search, so e.g. ``SATIS`` / ``TEVKIFAT`` / ``ISTISNA`` are never affected.
"""

from __future__ import annotations

from app.domain.invoice.dto import InternalInvoice

#: GİB UBL-TR ``InvoiceTypeCode`` values that denote a return document.
RETURN_INVOICE_TYPE_CODES = frozenset({"IADE", "TEVKIFATIADE", "YTBIADE", "YTBTEVKIFATIADE"})


def return_invoice_type_code(invoice: InternalInvoice) -> str | None:
    """The canonical return type code of ``invoice``, or ``None`` when it is not a return."""

    if not isinstance(invoice, InternalInvoice):
        return None
    code = invoice.header.invoice_type
    if code is None:
        return None
    canonical = code.strip().upper()
    return canonical if canonical in RETURN_INVOICE_TYPE_CODES else None


def is_return_invoice(invoice: InternalInvoice) -> bool:
    return return_invoice_type_code(invoice) is not None
