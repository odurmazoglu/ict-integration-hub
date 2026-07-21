from dataclasses import FrozenInstanceError
from datetime import date, time
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from app.domain.invoice import (
    Header,
    InternalInvoice,
    MonetaryTotals,
    Party,
    parse_ubl_invoice,
    validate_invoice,
)
from app.domain.invoice.exceptions import (
    InvalidInvoiceXmlError,
    MissingMandatoryInvoiceFieldError,
    UnsupportedInvoiceXmlError,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ubl"


def test_parse_minimal_ubl_invoice() -> None:
    invoice = parse_ubl_invoice((FIXTURES / "minimal_invoice.xml").read_bytes())

    assert invoice.header.invoice_number == "MIN202600001"
    assert invoice.header.invoice_uuid == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert invoice.header.ettn == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert invoice.header.issue_date == date(2026, 7, 20)
    assert invoice.header.currency_code == "TRY"
    assert invoice.supplier.name == "Supplier"
    assert invoice.customer.name == "Customer"
    assert invoice.totals.payable_amount == Decimal("1.00")
    assert len(invoice.lines) == 1
    assert invoice.lines[0].line_number == "1"
    assert invoice.lines[0].line_extension_amount == Decimal("1.00")
    assert invoice.attachments == ()


def test_parse_typical_ubl_invoice_extracts_header_parties_totals_lines_and_taxes() -> None:
    invoice = parse_ubl_invoice((FIXTURES / "valid_invoice.xml").read_text(encoding="utf-8"))

    assert invoice.header.profile_id == "TEMELFATURA"
    assert invoice.header.invoice_number == "SYN202600001"
    assert invoice.header.invoice_type == "SATIS"
    assert invoice.header.issue_time == time.fromisoformat("10:15:30+03:00")
    assert invoice.header.notes == ("Safe synthetic fixture.",)
    assert invoice.supplier.tax_number == "1111111111"
    assert invoice.supplier.tax_office == "Besiktas"
    assert invoice.supplier.emails == ("supplier@example.invalid",)
    assert invoice.supplier.phones == ("+900000000000",)
    assert invoice.supplier.addresses[0].city == "Istanbul"
    assert invoice.customer.name == "Synthetic Customer A.S."
    assert invoice.customer.addresses[0].city == "Ankara"
    assert invoice.totals.line_extension_amount == Decimal("250.00")
    assert invoice.totals.tax_exclusive_amount == Decimal("250.00")
    assert invoice.totals.tax_inclusive_amount == Decimal("286.00")
    assert invoice.totals.allowance_total == Decimal("5.50")
    assert invoice.totals.charge_total == Decimal("1.25")
    assert invoice.totals.payable_amount == Decimal("281.75")
    assert len(invoice.lines) == 2
    assert invoice.lines[0].description == "Synthetic consulting service"
    assert invoice.lines[0].quantity == Decimal("2.0000")
    assert invoice.lines[0].unit_code == "C62"
    assert invoice.lines[0].unit_price == Decimal("100.0000")
    assert invoice.lines[0].discounts[0].amount == Decimal("5.50")
    assert invoice.lines[0].taxes[0].tax_type == "KDV"
    assert invoice.lines[0].taxes[0].rate == Decimal("18")
    assert invoice.lines[0].taxes[0].base_amount == Decimal("200.00")
    assert invoice.lines[0].taxes[0].tax_amount == Decimal("36.00")
    assert invoice.lines[1].taxes[0].exemption_reason == "Istisna"


def test_missing_optional_fields_become_none_or_empty_collections() -> None:
    invoice = parse_ubl_invoice(_xml_with_body(""))

    assert invoice.header.invoice_type is None
    assert invoice.header.profile_id is None
    assert invoice.header.issue_date is None
    assert invoice.header.issue_time is None
    assert invoice.header.currency_code is None
    assert invoice.header.exchange_rate is None
    assert invoice.header.notes == ()
    assert invoice.supplier == Party()
    assert invoice.customer == Party()
    assert invoice.totals == MonetaryTotals()
    assert invoice.lines == ()
    assert invoice.attachments == ()


def test_multiple_invoice_lines_multiple_taxes_and_item_codes() -> None:
    invoice = parse_ubl_invoice(
        _xml_with_body(
            """
            <cbc:DocumentCurrencyCode>TRY</cbc:DocumentCurrencyCode>
            <cac:AccountingSupplierParty><cac:Party><cac:PartyName><cbc:Name>Supplier</cbc:Name></cac:PartyName></cac:Party></cac:AccountingSupplierParty>
            <cac:AccountingCustomerParty><cac:Party><cac:PartyName><cbc:Name>Customer</cbc:Name></cac:PartyName></cac:Party></cac:AccountingCustomerParty>
            <cac:LegalMonetaryTotal><cbc:PayableAmount>118.00</cbc:PayableAmount></cac:LegalMonetaryTotal>
            <cac:InvoiceLine>
              <cbc:ID>1</cbc:ID>
              <cbc:InvoicedQuantity unitCode="NIU">3</cbc:InvoicedQuantity>
              <cbc:LineExtensionAmount>100.00</cbc:LineExtensionAmount>
              <cac:Item>
                <cbc:Name>Product A</cbc:Name>
                <cac:SellersItemIdentification><cbc:ID>SELL-1</cbc:ID></cac:SellersItemIdentification>
                <cac:BuyersItemIdentification><cbc:ID>BUY-1</cbc:ID></cac:BuyersItemIdentification>
                <cac:StandardItemIdentification><cbc:ID>BAR-1</cbc:ID></cac:StandardItemIdentification>
              </cac:Item>
              <cac:Price><cbc:PriceAmount>33.33</cbc:PriceAmount></cac:Price>
              <cac:TaxTotal>
                <cac:TaxSubtotal>
                  <cbc:TaxableAmount>50.00</cbc:TaxableAmount>
                  <cbc:TaxAmount>5.00</cbc:TaxAmount>
                  <cbc:Percent>10</cbc:Percent>
                  <cac:TaxCategory><cac:TaxScheme><cbc:Name>KDV</cbc:Name></cac:TaxScheme></cac:TaxCategory>
                </cac:TaxSubtotal>
                <cac:TaxSubtotal>
                  <cbc:TaxableAmount>50.00</cbc:TaxableAmount>
                  <cbc:TaxAmount>13.00</cbc:TaxAmount>
                  <cbc:Percent>26</cbc:Percent>
                  <cac:TaxCategory><cac:TaxScheme><cbc:Name>OTV</cbc:Name></cac:TaxScheme></cac:TaxCategory>
                </cac:TaxSubtotal>
              </cac:TaxTotal>
            </cac:InvoiceLine>
            <cac:InvoiceLine><cbc:ID>2</cbc:ID><cac:Item><cbc:Name>Product B</cbc:Name></cac:Item></cac:InvoiceLine>
            """
        )
    )

    assert len(invoice.lines) == 2
    assert invoice.lines[0].seller_item_code == "SELL-1"
    assert invoice.lines[0].buyer_item_code == "BUY-1"
    assert invoice.lines[0].barcode == "BAR-1"
    assert len(invoice.lines[0].taxes) == 2
    assert {tax.tax_type for tax in invoice.lines[0].taxes} == {"KDV", "OTV"}
    assert invoice.lines[1].line_extension_amount is None


def test_exchange_rate_and_attachment_metadata_are_extracted_without_content() -> None:
    invoice = parse_ubl_invoice(
        _xml_with_body(
            """
            <cbc:DocumentCurrencyCode>USD</cbc:DocumentCurrencyCode>
            <cac:PricingExchangeRate><cbc:CalculationRate>32.50</cbc:CalculationRate></cac:PricingExchangeRate>
            <cac:AdditionalDocumentReference>
              <cbc:ID>ATT-1</cbc:ID>
              <cac:Attachment>
                <cbc:EmbeddedDocumentBinaryObject mimeCode="text/plain" filename="note.txt">
                  aGVsbG8=
                </cbc:EmbeddedDocumentBinaryObject>
              </cac:Attachment>
            </cac:AdditionalDocumentReference>
            """
        )
    )

    assert invoice.header.exchange_rate == Decimal("32.50")
    assert invoice.attachments[0].filename == "note.txt"
    assert invoice.attachments[0].mime_type == "text/plain"
    assert invoice.attachments[0].sha256 == sha256(b"hello").hexdigest()
    assert invoice.attachments[0].size == 5


def test_invalid_xml_raises_domain_exception() -> None:
    with pytest.raises(InvalidInvoiceXmlError):
        parse_ubl_invoice(b"<Invoice>")


def test_doctype_xml_raises_domain_exception() -> None:
    with pytest.raises(InvalidInvoiceXmlError):
        parse_ubl_invoice(b"<!DOCTYPE foo><Invoice/>")


def test_non_ubl_xml_raises_domain_exception() -> None:
    with pytest.raises(UnsupportedInvoiceXmlError):
        parse_ubl_invoice(b"<Invoice><ID>Not UBL</ID></Invoice>")


def test_missing_mandatory_invoice_identifiers_raise_domain_exception() -> None:
    with pytest.raises(MissingMandatoryInvoiceFieldError) as exc_info:
        parse_ubl_invoice(
            """<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
                 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
                 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
                 <cbc:UUID>aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee</cbc:UUID>
               </Invoice>"""
        )

    assert exc_info.value.field_path == "Invoice/cbc:ID"


def test_validation_helpers_report_missing_business_requirements() -> None:
    invoice = InternalInvoice(
        header=Header(invoice_number="", invoice_uuid=""),
        supplier=Party(),
        customer=Party(),
        totals=MonetaryTotals(),
    )

    issues = validate_invoice(invoice)

    assert {issue.field for issue in issues} == {
        "header.invoice_number",
        "header.invoice_uuid",
        "supplier",
        "customer",
        "lines",
        "header.currency_code",
    }


def test_internal_invoice_dtos_are_immutable() -> None:
    invoice = parse_ubl_invoice((FIXTURES / "minimal_invoice.xml").read_bytes())

    with pytest.raises(FrozenInstanceError):
        invoice.header.invoice_number = "changed"  # type: ignore[misc]


def test_domain_parser_has_no_provider_persistence_http_or_odoo_imports() -> None:
    domain_root = Path(__file__).resolve().parents[2] / "app" / "domain" / "invoice"
    source = "\n".join(path.read_text(encoding="utf-8") for path in domain_root.glob("*.py"))

    forbidden = (
        "connectors",
        "uyumsoft",
        "odoo",
        "zeep",
        "sqlalchemy",
        "fastapi",
        "requests",
        "httpx",
        "Session",
    )
    lowered = source.lower()
    for token in forbidden:
        assert token.lower() not in lowered


def _xml_with_body(body: str) -> str:
    return f"""<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
      <cbc:ID>SYN-001</cbc:ID>
      <cbc:UUID>00000000-0000-0000-0000-000000000001</cbc:UUID>
      {body}
    </Invoice>"""
