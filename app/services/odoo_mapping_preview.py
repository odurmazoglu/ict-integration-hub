import logging
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from app.schemas.normalized_invoice import (
    NormalizedInvoice,
    NormalizedInvoiceLine,
    NormalizedParty,
    NormalizedTaxSubtotal,
    NormalizedTaxTotal,
)
from app.schemas.odoo_mapping import (
    OdooDraftInvoicePayload,
    OdooInvoiceLinePayload,
    OdooJournalCandidate,
    OdooMappingIssue,
    OdooMappingPreview,
    OdooPartnerCandidate,
    OdooProductCandidate,
    OdooTaxCandidate,
)

logger = logging.getLogger(__name__)


class OdooMappingPreviewService:
    def build_preview(self, invoice: NormalizedInvoice) -> OdooMappingPreview:
        warnings: list[OdooMappingIssue] = []
        missing_fields: list[OdooMappingIssue] = []

        _validate_invoice(invoice, missing_fields=missing_fields, warnings=warnings)
        partner = _partner_candidate(invoice.supplier, missing_fields=missing_fields)
        taxes = _tax_candidates(invoice.tax_totals)
        if not taxes:
            missing_fields.append(_missing("missing_taxes", "Invoice has no tax mapping candidates.", "tax_totals"))

        lines = [
            _line_payload(line=line, sequence=index, missing_fields=missing_fields, warnings=warnings)
            for index, line in enumerate(invoice.lines, start=1)
        ]
        payload = OdooDraftInvoicePayload(
            move_type="in_invoice",
            invoice_date=invoice.issue_datetime.date() if invoice.issue_datetime else None,
            currency=_normalized_currency(invoice.document_currency),
            journal=_journal_candidate(invoice.document_currency),
            partner=partner,
            invoice_lines=lines,
            taxes=taxes,
            references=_references(invoice),
            notes=invoice.notes,
            payment_terms=None,
            invoice_number=invoice.invoice_number or None,
            ettn=invoice.ettn or None,
        )
        status = "ready" if not missing_fields else "needs_review"
        preview = OdooMappingPreview(
            invoice=payload,
            lines=lines,
            warnings=warnings,
            missing_fields=missing_fields,
            mapping_status=status,
        )
        logger.info(
            "odoo_mapping_preview_completed",
            extra={
                "invoice_id": invoice.invoice_number or None,
                "mapping_status": preview.mapping_status,
                "warning_count": len(preview.warnings),
                "line_count": len(preview.lines),
            },
        )
        return preview


def _validate_invoice(
    invoice: NormalizedInvoice,
    *,
    missing_fields: list[OdooMappingIssue],
    warnings: list[OdooMappingIssue],
) -> None:
    if not invoice.invoice_number.strip():
        missing_fields.append(_missing("missing_invoice_number", "Invoice number is required.", "invoice_number"))
    if not invoice.ettn.strip():
        missing_fields.append(_missing("missing_ettn", "ETTN is required for traceability.", "ettn"))
    if not _normalized_currency(invoice.document_currency):
        missing_fields.append(_missing("missing_currency", "Document currency is required.", "document_currency"))
    if not _is_timezone_aware(invoice.issue_datetime):
        missing_fields.append(_missing("missing_timezone", "Issue datetime must be timezone-aware.", "issue_datetime"))
    if not invoice.lines:
        missing_fields.append(_missing("missing_invoice_lines", "At least one invoice line is required.", "lines"))
    if invoice.profile_id:
        warnings.append(_warning("unsupported_profile_id", "Profile ID is preserved as reference only.", "profile_id"))


def _partner_candidate(
    party: NormalizedParty,
    *,
    missing_fields: list[OdooMappingIssue],
) -> OdooPartnerCandidate | None:
    if not party.tax_id and not party.party_name:
        missing_fields.append(
            _missing("missing_partner", "Supplier tax id or name is required for partner matching.", "supplier")
        )
        return None
    lookup_key = "tax_id" if party.tax_id else "name"
    if not party.tax_id:
        missing_fields.append(
            _missing(
                "missing_partner_tax_id",
                "Supplier tax id is required for deterministic partner matching.",
                "supplier.tax_id",
            )
        )
    return OdooPartnerCandidate(
        role="supplier",
        lookup_key=lookup_key,
        name=party.party_name,
        tax_id=party.tax_id,
        tax_office=party.tax_office,
        email=party.contact.email if party.contact else None,
        phone=party.contact.telephone if party.contact else None,
        city=party.address.city_name if party.address else None,
        country=party.address.country if party.address else None,
    )


def _line_payload(
    *,
    line: NormalizedInvoiceLine,
    sequence: int,
    missing_fields: list[OdooMappingIssue],
    warnings: list[OdooMappingIssue],
) -> OdooInvoiceLinePayload:
    field_prefix = f"lines[{sequence - 1}]"
    description = _line_description(line)
    product = _product_candidate(line)
    if not description:
        missing_fields.append(_missing("missing_line_description", "Line description is required.", field_prefix))
        description = f"Line {line.line_id}"
    if product is None:
        missing_fields.append(
            _missing("missing_product", "Line product candidate is required.", f"{field_prefix}.product")
        )
    if line.quantity is None:
        missing_fields.append(
            _missing("missing_line_quantity", "Line quantity is required.", f"{field_prefix}.quantity")
        )
    unit_price = line.unit_price
    if unit_price is None and line.quantity and line.quantity != Decimal("0"):
        unit_price = line.line_extension_amount / line.quantity
        warnings.append(
            _warning("derived_unit_price", "Line unit price was derived from amount and quantity.", field_prefix)
        )
    if unit_price is None:
        missing_fields.append(
            _missing("missing_unit_price", "Line unit price is required.", f"{field_prefix}.unit_price")
        )
    taxes = _tax_candidates(line.tax_totals)
    if not taxes:
        missing_fields.append(
            _missing("missing_line_tax", "Line has no tax mapping candidates.", f"{field_prefix}.taxes")
        )
    return OdooInvoiceLinePayload(
        sequence=sequence,
        product=product,
        description=description,
        quantity=line.quantity,
        unit_price=unit_price,
        unit_of_measure=line.unit_code,
        taxes=taxes,
        line_extension_amount=line.line_extension_amount,
    )


def _line_description(line: NormalizedInvoiceLine) -> str:
    parts = [part.strip() for part in (line.item_name, line.description) if part and part.strip()]
    return " - ".join(parts)


def _product_candidate(line: NormalizedInvoiceLine) -> OdooProductCandidate | None:
    name = line.item_name or line.description
    if name is None or not name.strip():
        return None
    return OdooProductCandidate(lookup_key="name", name=name.strip())


def _journal_candidate(currency: str) -> OdooJournalCandidate | None:
    normalized_currency = _normalized_currency(currency)
    if normalized_currency is None:
        return None
    return OdooJournalCandidate(journal_type="purchase", currency=normalized_currency)


def _tax_candidates(tax_totals: Iterable[NormalizedTaxTotal]) -> list[OdooTaxCandidate]:
    seen: set[tuple[str | None, Decimal | None, str | None]] = set()
    candidates: list[OdooTaxCandidate] = []
    for subtotal in _tax_subtotals(tax_totals):
        key = (subtotal.tax_category, subtotal.percent, subtotal.exemption_reason_code)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            OdooTaxCandidate(
                name=subtotal.tax_category,
                percent=subtotal.percent,
                exemption_reason_code=subtotal.exemption_reason_code,
                exemption_reason=subtotal.exemption_reason,
            )
        )
    return candidates


def _tax_subtotals(tax_totals: Iterable[NormalizedTaxTotal]) -> Iterable[NormalizedTaxSubtotal]:
    for total in tax_totals:
        yield from total.subtotals


def _references(invoice: NormalizedInvoice) -> list[str]:
    references = [invoice.invoice_number, invoice.ettn, *invoice.references]
    return [reference for reference in references if reference and reference.strip()]


def _normalized_currency(currency: str) -> str | None:
    normalized = currency.strip().upper()
    return normalized or None


def _is_timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _missing(code: str, message: str, field_path: str) -> OdooMappingIssue:
    return OdooMappingIssue(code=code, message=message, field_path=field_path, severity="missing_field")


def _warning(code: str, message: str, field_path: str) -> OdooMappingIssue:
    return OdooMappingIssue(code=code, message=message, field_path=field_path, severity="warning")
