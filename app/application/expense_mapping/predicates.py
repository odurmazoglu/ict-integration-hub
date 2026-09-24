from __future__ import annotations

from app.domain.invoice import InternalInvoice


def _is_blank(value: str | None) -> bool:
    return value is None or not value.strip()


def invoice_is_product_identifier_free(invoice: InternalInvoice) -> bool:
    """True only when every invoice line lacks all deterministic product identifiers.

    A line is identifier-free when ``buyer_item_code``, ``seller_item_code`` and
    ``barcode`` are all absent (``None`` or whitespace-only). An invoice with no
    lines is not identifier-free. If any line carries any identifier, the
    operating-expense branch is not eligible and a failed product match stays a
    Manual Review reason.
    """

    if not isinstance(invoice, InternalInvoice) or not invoice.lines:
        return False
    return all(
        _is_blank(line.buyer_item_code) and _is_blank(line.seller_item_code) and _is_blank(line.barcode)
        for line in invoice.lines
    )


def invoice_has_product_identifier(invoice: InternalInvoice) -> bool:
    """True when at least one invoice line carries a deterministic product identifier.

    The same identifiers (``buyer_item_code``, ``seller_item_code``, ``barcode``) that
    drive product matching and ``invoice_is_product_identifier_free``. An invoice with
    no lines has no product identifier (P0-PROD-18E-1B product-shaped RESALE check).
    """

    if not isinstance(invoice, InternalInvoice):
        return False
    return any(
        not (_is_blank(line.buyer_item_code) and _is_blank(line.seller_item_code) and _is_blank(line.barcode))
        for line in invoice.lines
    )
