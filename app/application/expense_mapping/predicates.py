from __future__ import annotations

from app.domain.invoice import InternalInvoice, InvoiceLine


def _is_blank(value: str | None) -> bool:
    return value is None or not value.strip()


def _line_is_identifier_free(line: InvoiceLine) -> bool:
    """True when the line carries none of the fields that gate the product path.

    ``commodity_classification`` is not a product identifier and is never used for product
    lookup. It is still counted here because, before P0-PROD-19A-1, the parser folded it
    into ``barcode``; keeping it preserves the existing operating-expense / RESALE routing
    boundary exactly. Relaxing that is a separate business decision.

    ``manufacturer_item_code`` is deliberately not counted yet: it was never parsed before
    19A-1 and no matcher consumes it, so counting it would move routing without a lookup.
    """

    return (
        _is_blank(line.buyer_item_code)
        and _is_blank(line.seller_item_code)
        and _is_blank(line.barcode)
        and _is_blank(line.commodity_classification)
    )


def invoice_is_product_identifier_free(invoice: InternalInvoice) -> bool:
    """True only when every invoice line lacks all deterministic product identifiers.

    A line is identifier-free when ``buyer_item_code``, ``seller_item_code``, ``barcode`` and
    ``commodity_classification`` are all absent (``None`` or whitespace-only). An invoice
    with no lines is not identifier-free. If any line carries any identifier, the
    operating-expense branch is not eligible and a failed product match stays a Manual
    Review reason.
    """

    if not isinstance(invoice, InternalInvoice) or not invoice.lines:
        return False
    return all(_line_is_identifier_free(line) for line in invoice.lines)


def invoice_has_product_identifier(invoice: InternalInvoice) -> bool:
    """True when at least one invoice line carries a deterministic product identifier.

    The same fields as ``invoice_is_product_identifier_free``. An invoice with no lines has
    no product identifier (P0-PROD-18E-1B product-shaped RESALE check).
    """

    if not isinstance(invoice, InternalInvoice):
        return False
    return any(not _line_is_identifier_free(line) for line in invoice.lines)
