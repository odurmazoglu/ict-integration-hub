from __future__ import annotations

from app.application.rules.classification import InvoiceClassificationContext
from app.domain.invoice import InternalInvoice
from app.matching import InvoiceProductMatchResult, PartnerMatchResult, PartnerMatchStatus, ProductMatchStatus


def build_invoice_classification_context(
    *,
    invoice: InternalInvoice,
    company_id: int,
    partner_match: PartnerMatchResult | None = None,
    product_match: InvoiceProductMatchResult | None = None,
) -> InvoiceClassificationContext:
    """Build canonical classification evidence from already-available Hub data."""

    line_descriptions = tuple(line.description for line in invoice.lines if line.description)
    return InvoiceClassificationContext.from_line_descriptions(
        company_id=company_id,
        vendor_partner_id=_matched_partner_id(partner_match),
        vendor_tax_id=invoice.supplier.tax_number,
        currency=invoice.header.currency_code,
        provider_document_type=invoice.header.invoice_type or invoice.header.profile_id,
        purchase_order_present=None,
        line_descriptions=line_descriptions,
        product_mapping_ids=_matched_product_ids(product_match),
    )


def _matched_partner_id(partner_match: PartnerMatchResult | None) -> int | None:
    if partner_match is None:
        return None
    if partner_match.status is not PartnerMatchStatus.MATCHED:
        return None
    return partner_match.partner_id


def _matched_product_ids(product_match: InvoiceProductMatchResult | None) -> tuple[int, ...]:
    if product_match is None:
        return ()
    product_ids = {
        line_result.result.product_id
        for line_result in product_match.line_results
        if line_result.result.status is ProductMatchStatus.MATCHED and line_result.result.product_id is not None
    }
    return tuple(sorted(product_ids))
