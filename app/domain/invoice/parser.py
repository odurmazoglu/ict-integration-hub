from __future__ import annotations

import base64
import hashlib
from datetime import date, time
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

from app.domain.invoice.dto import (
    Address,
    Attachment,
    Discount,
    Header,
    InternalInvoice,
    InvoiceLine,
    MonetaryTotals,
    Party,
    Tax,
)
from app.domain.invoice.exceptions import (
    InvalidInvoiceXmlError,
    MissingMandatoryInvoiceFieldError,
    UnsupportedInvoiceXmlError,
)

UBL_INVOICE_NAMESPACE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
NS = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}
XML_SAFETY_MARKERS = (b"<!DOCTYPE", b"<!ENTITY")


def parse_ubl_invoice(content: bytes | str) -> InternalInvoice:
    root = _parse_xml(content)
    _require_ubl_invoice_root(root)
    invoice_number = _required_text(root, "cbc:ID", "Invoice/cbc:ID")
    invoice_uuid = _required_text(root, "cbc:UUID", "Invoice/cbc:UUID")
    header = Header(
        invoice_number=invoice_number,
        invoice_uuid=invoice_uuid,
        ettn=invoice_uuid,
        invoice_type=_optional_text(root, "cbc:InvoiceTypeCode"),
        profile_id=_optional_text(root, "cbc:ProfileID"),
        issue_date=_optional_date(root, "cbc:IssueDate", "Invoice/cbc:IssueDate"),
        issue_time=_optional_time(root, "cbc:IssueTime", "Invoice/cbc:IssueTime"),
        currency_code=_optional_text(root, "cbc:DocumentCurrencyCode"),
        exchange_rate=_exchange_rate(root),
        notes=_texts(root, "cbc:Note"),
    )
    return InternalInvoice(
        header=header,
        supplier=_party(root.find("cac:AccountingSupplierParty/cac:Party", NS)),
        customer=_party(root.find("cac:AccountingCustomerParty/cac:Party", NS)),
        totals=_monetary_totals(root.find("cac:LegalMonetaryTotal", NS)),
        lines=tuple(_invoice_line(line) for line in root.findall("cac:InvoiceLine", NS)),
        attachments=_attachments(root),
    )


def _parse_xml(content: bytes | str) -> ElementTree.Element:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    if any(marker in raw.upper() for marker in XML_SAFETY_MARKERS):
        raise InvalidInvoiceXmlError("XML document type declarations and entities are not supported.")
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise InvalidInvoiceXmlError("Malformed invoice XML.") from exc


def _require_ubl_invoice_root(root: ElementTree.Element) -> None:
    namespace, local_name = _split_tag(root.tag)
    if local_name != "Invoice" or namespace != UBL_INVOICE_NAMESPACE:
        raise UnsupportedInvoiceXmlError("XML document is not a supported UBL invoice.", field_path="/")


def _party(party: ElementTree.Element | None) -> Party:
    if party is None:
        return Party()
    contacts = party.findall("cac:Contact", NS)
    return Party(
        name=_first_text(
            party,
            (
                "cac:PartyName/cbc:Name",
                "cac:PartyLegalEntity/cbc:RegistrationName",
            ),
        ),
        tax_number=_first_text(
            party,
            (
                "cac:PartyIdentification/cbc:ID",
                "cac:PartyTaxScheme/cbc:CompanyID",
                "cac:PartyLegalEntity/cbc:CompanyID",
            ),
        ),
        tax_office=_optional_text(party, "cac:PartyTaxScheme/cac:TaxScheme/cbc:Name"),
        mersis_number=_mersis_number(party),
        website=_optional_text(party, "cbc:WebsiteURI"),
        emails=tuple(item for item in (_optional_text(contact, "cbc:ElectronicMail") for contact in contacts) if item),
        phones=tuple(item for item in (_optional_text(contact, "cbc:Telephone") for contact in contacts) if item),
        addresses=tuple(_address(address) for address in party.findall("cac:PostalAddress", NS)),
    )


def _address(address: ElementTree.Element) -> Address:
    return Address(
        street=_optional_text(address, "cbc:StreetName"),
        building_number=_optional_text(address, "cbc:BuildingNumber"),
        city=_optional_text(address, "cbc:CityName"),
        district=_optional_text(address, "cbc:CitySubdivisionName"),
        postal_code=_optional_text(address, "cbc:PostalZone"),
        country=_optional_text(address, "cac:Country/cbc:Name")
        or _optional_text(address, "cac:Country/cbc:IdentificationCode"),
    )


def _monetary_totals(total: ElementTree.Element | None) -> MonetaryTotals:
    if total is None:
        return MonetaryTotals()
    return MonetaryTotals(
        line_extension_amount=_optional_decimal(total, "cbc:LineExtensionAmount"),
        tax_exclusive_amount=_optional_decimal(total, "cbc:TaxExclusiveAmount"),
        tax_inclusive_amount=_optional_decimal(total, "cbc:TaxInclusiveAmount"),
        allowance_total=_optional_decimal(total, "cbc:AllowanceTotalAmount"),
        charge_total=_optional_decimal(total, "cbc:ChargeTotalAmount"),
        payable_amount=_optional_decimal(total, "cbc:PayableAmount"),
        rounding_amount=_optional_decimal(total, "cbc:PayableRoundingAmount"),
    )


def _invoice_line(line: ElementTree.Element) -> InvoiceLine:
    quantity = line.find("cbc:InvoicedQuantity", NS)
    item = line.find("cac:Item", NS)
    return InvoiceLine(
        line_number=_optional_text(line, "cbc:ID"),
        description=_line_description(item),
        seller_item_code=_optional_text(item, "cac:SellersItemIdentification/cbc:ID"),
        buyer_item_code=_optional_text(item, "cac:BuyersItemIdentification/cbc:ID"),
        barcode=_first_text(
            item,
            (
                "cac:StandardItemIdentification/cbc:ID",
                "cac:CommodityClassification/cbc:ItemClassificationCode",
            ),
        )
        if item is not None
        else None,
        quantity=_decimal_from_element(quantity, "Invoice/cac:InvoiceLine/cbc:InvoicedQuantity")
        if quantity is not None
        else None,
        unit_code=quantity.attrib.get("unitCode") if quantity is not None else None,
        unit_price=_optional_decimal(line, "cac:Price/cbc:PriceAmount"),
        line_extension_amount=_optional_decimal(line, "cbc:LineExtensionAmount"),
        discounts=tuple(_discount(item) for item in line.findall("cac:AllowanceCharge", NS) if _is_discount(item)),
        taxes=_taxes(line.findall("cac:TaxTotal", NS)),
    )


def _line_description(item: ElementTree.Element | None) -> str | None:
    if item is None:
        return None
    return _optional_text(item, "cbc:Description") or _optional_text(item, "cbc:Name")


def _discount(element: ElementTree.Element) -> Discount:
    return Discount(
        amount=_optional_decimal(element, "cbc:Amount"),
        reason=_optional_text(element, "cbc:AllowanceChargeReason"),
        rate=_optional_decimal(element, "cbc:MultiplierFactorNumeric"),
    )


def _is_discount(element: ElementTree.Element) -> bool:
    return (_optional_text(element, "cbc:ChargeIndicator") or "").strip().lower() == "false"


def _taxes(elements: list[ElementTree.Element]) -> tuple[Tax, ...]:
    taxes: list[Tax] = []
    for total in elements:
        subtotals = total.findall("cac:TaxSubtotal", NS)
        if not subtotals:
            taxes.append(
                Tax(
                    tax_amount=_optional_decimal(total, "cbc:TaxAmount"),
                )
            )
            continue
        taxes.extend(_tax_from_subtotal(subtotal) for subtotal in subtotals)
    return tuple(taxes)


def _tax_from_subtotal(subtotal: ElementTree.Element) -> Tax:
    category = subtotal.find("cac:TaxCategory", NS)
    return Tax(
        tax_type=_optional_text(category, "cac:TaxScheme/cbc:Name") if category is not None else None,
        rate=_optional_decimal(subtotal, "cbc:Percent"),
        base_amount=_optional_decimal(subtotal, "cbc:TaxableAmount"),
        tax_amount=_optional_decimal(subtotal, "cbc:TaxAmount"),
        exemption_reason=_first_text(
            category,
            (
                "cbc:TaxExemptionReason",
                "cbc:TaxExemptionReasonCode",
            ),
        )
        if category is not None
        else None,
    )


def _attachments(root: ElementTree.Element) -> tuple[Attachment, ...]:
    attachments: list[Attachment] = []
    for reference in root.findall("cac:AdditionalDocumentReference", NS):
        attachment = reference.find("cac:Attachment", NS)
        embedded = attachment.find("cbc:EmbeddedDocumentBinaryObject", NS) if attachment is not None else None
        filename = (embedded.attrib.get("filename") if embedded is not None else None) or _optional_text(
            reference,
            "cbc:ID",
        )
        mime_type = embedded.attrib.get("mimeCode") if embedded is not None else None
        decoded = _embedded_bytes(embedded)
        attachments.append(
            Attachment(
                filename=filename,
                mime_type=mime_type,
                sha256=hashlib.sha256(decoded).hexdigest() if decoded is not None else None,
                size=len(decoded) if decoded is not None else None,
            )
        )
    return tuple(attachments)


def _embedded_bytes(element: ElementTree.Element | None) -> bytes | None:
    if element is None or element.text is None or not element.text.strip():
        return None
    try:
        return base64.b64decode(element.text.strip(), validate=True)
    except ValueError:
        return None


def _exchange_rate(root: ElementTree.Element) -> Decimal | None:
    for path in (
        "cac:PricingExchangeRate/cbc:CalculationRate",
        "cac:PaymentExchangeRate/cbc:CalculationRate",
        "cac:TaxExchangeRate/cbc:CalculationRate",
    ):
        value = _optional_decimal(root, path)
        if value is not None:
            return value
    return None


def _mersis_number(party: ElementTree.Element) -> str | None:
    for item in party.findall("cac:PartyIdentification/cbc:ID", NS):
        scheme = (item.attrib.get("schemeID") or "").strip().lower()
        if scheme == "mersis" and item.text and item.text.strip():
            return item.text.strip()
    return None


def _required_text(element: ElementTree.Element, path: str, field_path: str) -> str:
    value = _optional_text(element, path)
    if value is None:
        raise MissingMandatoryInvoiceFieldError(
            f"Missing mandatory invoice identifier {field_path}.",
            field_path=field_path,
        )
    return value


def _optional_text(element: ElementTree.Element | None, path: str) -> str | None:
    if element is None:
        return None
    child = element.find(path, NS)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _texts(element: ElementTree.Element, path: str) -> tuple[str, ...]:
    return tuple(child.text.strip() for child in element.findall(path, NS) if child.text and child.text.strip())


def _first_text(element: ElementTree.Element | None, paths: tuple[str, ...]) -> str | None:
    if element is None:
        return None
    for path in paths:
        value = _optional_text(element, path)
        if value is not None:
            return value
    return None


def _optional_decimal(element: ElementTree.Element | None, path: str) -> Decimal | None:
    if element is None:
        return None
    child = element.find(path, NS)
    if child is None:
        return None
    return _decimal_from_element(child, path)


def _decimal_from_element(element: ElementTree.Element, field_path: str) -> Decimal | None:
    text = (element.text or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise InvalidInvoiceXmlError(f"Invalid Decimal value at {field_path}.", field_path=field_path) from exc


def _optional_date(element: ElementTree.Element, path: str, field_path: str) -> date | None:
    text = _optional_text(element, path)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise InvalidInvoiceXmlError(f"Invalid date value at {field_path}.", field_path=field_path) from exc


def _optional_time(element: ElementTree.Element, path: str, field_path: str) -> time | None:
    text = _optional_text(element, path)
    if text is None:
        return None
    try:
        parsed = time.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidInvoiceXmlError(f"Invalid time value at {field_path}.", field_path=field_path) from exc
    return parsed


def _split_tag(tag: str) -> tuple[str | None, str]:
    if tag.startswith("{"):
        namespace, _, local_name = tag[1:].partition("}")
        return namespace, local_name
    return None, tag
