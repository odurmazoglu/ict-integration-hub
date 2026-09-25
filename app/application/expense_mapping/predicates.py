from __future__ import annotations

from app.domain.invoice import InternalInvoice, InvoiceLine
from app.domain.invoice.source_profiles import profile_manufacturer_sku


def _is_blank(value: str | None) -> bool:
    return value is None or not value.strip()


def _line_is_identifier_free(invoice: InternalInvoice, line: InvoiceLine) -> bool:
    """True when the line carries none of the fields that gate the product path.

    Every identity the product matcher looks up is counted, including the authoritative
    manufacturer SKUs of P0-PROD-19A-2 (``manufacturer_item_code`` and a supplier
    source-profile Description SKU), so routing never treats a line as identifier-free
    while the matcher still performs a lookup for it.

    ``commodity_classification`` is not a product identifier and is never used for product
    lookup. It is still counted here because, before P0-PROD-19A-1, the parser folded it
    into ``barcode``; keeping it preserves the existing operating-expense / RESALE routing
    boundary exactly. Relaxing that is a separate business decision.
    """

    return (
        _is_blank(line.buyer_item_code)
        and _is_blank(line.seller_item_code)
        and _is_blank(line.barcode)
        and _is_blank(line.commodity_classification)
        and _is_blank(line.manufacturer_item_code)
        and profile_manufacturer_sku(invoice, line) is None
    )


def invoice_is_product_identifier_free(invoice: InternalInvoice) -> bool:
    """True only when every invoice line lacks all deterministic product identifiers.

    A line is identifier-free when ``buyer_item_code``, ``seller_item_code``, ``barcode``,
    ``commodity_classification`` and ``manufacturer_item_code`` are all absent (``None`` or
    whitespace-only) and no supplier source profile reads a manufacturer SKU from it. An invoice
    with no lines is not identifier-free. If any line carries any identifier, the
    operating-expense branch is not eligible and a failed product match stays a Manual
    Review reason.
    """

    if not isinstance(invoice, InternalInvoice) or not invoice.lines:
        return False
    return all(_line_is_identifier_free(invoice, line) for line in invoice.lines)


def invoice_has_product_identifier(invoice: InternalInvoice) -> bool:
    """True when at least one invoice line carries a deterministic product identifier.

    The same fields as ``invoice_is_product_identifier_free``. An invoice with no lines has
    no product identifier (P0-PROD-18E-1B product-shaped RESALE check).
    """

    if not isinstance(invoice, InternalInvoice):
        return False
    return any(not _line_is_identifier_free(invoice, line) for line in invoice.lines)
