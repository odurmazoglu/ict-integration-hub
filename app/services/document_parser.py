import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from time import perf_counter
from typing import Any
from xml.etree import ElementTree

from sqlalchemy.orm import Session

from app.models.invoice_document import InvoiceDocument
from app.schemas.invoice_document import DocumentType
from app.schemas.normalized_invoice import (
    NormalizedAddress,
    NormalizedAllowanceCharge,
    NormalizedContact,
    NormalizedInvoice,
    NormalizedInvoiceLine,
    NormalizedMonetaryTotals,
    NormalizedParty,
    NormalizedTaxSubtotal,
    NormalizedTaxTotal,
)
from app.services.document_storage import DocumentStorage, DocumentStorageError

logger = logging.getLogger(__name__)

DOCUMENT_TYPE_UBL_XML: DocumentType = "UBL_XML"
XML_SAFETY_MARKERS = (b"<!DOCTYPE", b"<!ENTITY")
NS = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}


class DocumentParseError(Exception):
    category = "parse_error"

    def __init__(self, safe_message: str, *, field_path: str | None = None) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.field_path = field_path


class MalformedXmlError(DocumentParseError):
    category = "malformed_xml"


class MissingRequiredFieldError(DocumentParseError):
    category = "missing_required_field"


class InvalidDecimalValueError(DocumentParseError):
    category = "invalid_decimal"


class InvalidDateTimeValueError(DocumentParseError):
    category = "invalid_datetime"


class UnsupportedInvoiceStructureError(DocumentParseError):
    category = "unsupported_invoice_structure"


class UnsupportedDocumentTypeError(DocumentParseError):
    category = "unsupported_document_type"


class DocumentReadError(DocumentParseError):
    category = "storage_read_failure"


@dataclass(frozen=True)
class DocumentParseDiagnostic:
    document_id: int
    storage_key: str
    category: str
    safe_message: str
    field_path: str | None = None


@dataclass(frozen=True)
class DocumentParseResult:
    document_id: int
    storage_key: str
    invoice: NormalizedInvoice
    diagnostics: list[DocumentParseDiagnostic]


class InvoiceDocumentParseService:
    def __init__(self, *, session: Session, storage: DocumentStorage) -> None:
        self._session = session
        self._storage = storage

    def parse_document(self, document_id: int) -> DocumentParseResult:
        document = self._get_document(document_id)
        started = perf_counter()
        try:
            content = self._storage.read(document.storage_key)
        except DocumentStorageError as exc:
            raise DocumentReadError("Document storage read failed.") from exc

        try:
            invoice = UblInvoiceParser().parse(content)
        except DocumentParseError as exc:
            logger.info(
                "invoice_document_parse_failed",
                extra=_safe_log_extra(document=document, started=started, result="failure", category=exc.category),
            )
            raise

        logger.info(
            "invoice_document_parse_completed",
            extra=_safe_log_extra(document=document, started=started, result="success", category=None),
        )
        return DocumentParseResult(
            document_id=document.id,
            storage_key=document.storage_key,
            invoice=invoice,
            diagnostics=[],
        )

    def _get_document(self, document_id: int) -> InvoiceDocument:
        document = self._session.get(InvoiceDocument, document_id)
        if document is None:
            raise MissingRequiredFieldError("Invoice document metadata was not found.", field_path="document_id")
        if document.document_type != DOCUMENT_TYPE_UBL_XML:
            raise UnsupportedDocumentTypeError("Only UBL_XML documents can be parsed.", field_path="document_type")
        return document


class UblInvoiceParser:
    def parse(self, content: bytes) -> NormalizedInvoice:
        root = _parse_xml(content)
        _require_invoice_root(root)
        issue_datetime = _issue_datetime(root)
        return NormalizedInvoice(
            invoice_number=_required_text(root, "cbc:ID", "Invoice/cbc:ID"),
            ettn=_required_text(root, "cbc:UUID", "Invoice/cbc:UUID"),
            invoice_type_code=_optional_text(root, "cbc:InvoiceTypeCode"),
            profile_id=_optional_text(root, "cbc:ProfileID"),
            issue_datetime=issue_datetime,
            document_currency=_required_text(root, "cbc:DocumentCurrencyCode", "Invoice/cbc:DocumentCurrencyCode"),
            notes=_texts(root, "cbc:Note"),
            references=_references(root),
            supplier=_party(root, "cac:AccountingSupplierParty", "supplier"),
            customer=_party(root, "cac:AccountingCustomerParty", "customer"),
            monetary_totals=_monetary_totals(root),
            tax_totals=_tax_totals(root.findall("cac:TaxTotal", NS), "Invoice/cac:TaxTotal"),
            lines=_invoice_lines(root),
        )


def _parse_xml(content: bytes) -> ElementTree.Element:
    if any(marker in content.upper() for marker in XML_SAFETY_MARKERS):
        raise MalformedXmlError("XML document type declarations and entities are not supported.")
    try:
        return ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise MalformedXmlError("Malformed XML document.") from exc


def _require_invoice_root(root: ElementTree.Element) -> None:
    if _local_name(root.tag) != "Invoice":
        raise UnsupportedInvoiceStructureError("Only UBL Invoice documents are supported.", field_path="/")


def _issue_datetime(root: ElementTree.Element) -> datetime:
    issue_date = _required_text(root, "cbc:IssueDate", "Invoice/cbc:IssueDate")
    issue_time = _optional_text(root, "cbc:IssueTime")
    try:
        parsed_date = date.fromisoformat(issue_date)
    except ValueError as exc:
        raise InvalidDateTimeValueError("Invalid invoice issue date.", field_path="Invoice/cbc:IssueDate") from exc
    if not issue_time:
        return datetime.combine(parsed_date, time.min, tzinfo=UTC)
    try:
        parsed_time = time.fromisoformat(issue_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidDateTimeValueError("Invalid invoice issue time.", field_path="Invoice/cbc:IssueTime") from exc
    if parsed_time.tzinfo is None:
        parsed_time = parsed_time.replace(tzinfo=UTC)
    return datetime.combine(parsed_date, parsed_time)


def _party(root: ElementTree.Element, path: str, label: str) -> NormalizedParty:
    party = root.find(f"{path}/cac:Party", NS)
    if party is None:
        raise MissingRequiredFieldError(f"Missing {label} party.", field_path=f"Invoice/{path}/cac:Party")
    return NormalizedParty(
        tax_id=_party_tax_id(party),
        party_name=_first_text(party, ("cac:PartyName/cbc:Name", "cac:PartyLegalEntity/cbc:RegistrationName")),
        tax_office=_optional_text(party, "cac:PartyTaxScheme/cac:TaxScheme/cbc:Name"),
        address=_address(party.find("cac:PostalAddress", NS)),
        contact=_contact(party.find("cac:Contact", NS)),
    )


def _party_tax_id(party: ElementTree.Element) -> str | None:
    return _first_text(
        party,
        (
            "cac:PartyIdentification/cbc:ID",
            "cac:PartyTaxScheme/cbc:CompanyID",
            "cac:PartyLegalEntity/cbc:CompanyID",
        ),
    )


def _address(address: ElementTree.Element | None) -> NormalizedAddress | None:
    if address is None:
        return None
    return NormalizedAddress(
        street_name=_optional_text(address, "cbc:StreetName"),
        building_number=_optional_text(address, "cbc:BuildingNumber"),
        city_subdivision_name=_optional_text(address, "cbc:CitySubdivisionName"),
        city_name=_optional_text(address, "cbc:CityName"),
        postal_zone=_optional_text(address, "cbc:PostalZone"),
        country=_optional_text(address, "cac:Country/cbc:Name"),
    )


def _contact(contact: ElementTree.Element | None) -> NormalizedContact | None:
    if contact is None:
        return None
    return NormalizedContact(
        telephone=_optional_text(contact, "cbc:Telephone"),
        telefax=_optional_text(contact, "cbc:Telefax"),
        email=_optional_text(contact, "cbc:ElectronicMail"),
    )


def _monetary_totals(root: ElementTree.Element) -> NormalizedMonetaryTotals:
    total = root.find("cac:LegalMonetaryTotal", NS)
    if total is None:
        raise MissingRequiredFieldError("Missing legal monetary total.", field_path="Invoice/cac:LegalMonetaryTotal")
    return NormalizedMonetaryTotals(
        line_extension_amount=_optional_decimal(total, "cbc:LineExtensionAmount"),
        tax_exclusive_amount=_optional_decimal(total, "cbc:TaxExclusiveAmount"),
        tax_inclusive_amount=_optional_decimal(total, "cbc:TaxInclusiveAmount"),
        allowance_total_amount=_optional_decimal(total, "cbc:AllowanceTotalAmount"),
        charge_total_amount=_optional_decimal(total, "cbc:ChargeTotalAmount"),
        payable_amount=_required_decimal(
            total, "cbc:PayableAmount", "Invoice/cac:LegalMonetaryTotal/cbc:PayableAmount"
        ),
    )


def _tax_totals(elements: list[ElementTree.Element], path: str) -> list[NormalizedTaxTotal]:
    return [
        NormalizedTaxTotal(
            total_tax_amount=_required_decimal(element, "cbc:TaxAmount", f"{path}/cbc:TaxAmount"),
            subtotals=[
                _tax_subtotal(subtotal, f"{path}/cac:TaxSubtotal")
                for subtotal in element.findall("cac:TaxSubtotal", NS)
            ],
        )
        for element in elements
    ]


def _tax_subtotal(element: ElementTree.Element, path: str) -> NormalizedTaxSubtotal:
    category = element.find("cac:TaxCategory", NS)
    return NormalizedTaxSubtotal(
        tax_category=_optional_text(category, "cac:TaxScheme/cbc:Name") if category is not None else None,
        percent=_optional_decimal(element, "cbc:Percent"),
        taxable_amount=_optional_decimal(element, "cbc:TaxableAmount"),
        tax_amount=_required_decimal(element, "cbc:TaxAmount", f"{path}/cbc:TaxAmount"),
        exemption_reason=_optional_text(category, "cbc:TaxExemptionReason") if category is not None else None,
        exemption_reason_code=_optional_text(category, "cbc:TaxExemptionReasonCode") if category is not None else None,
    )


def _invoice_lines(root: ElementTree.Element) -> list[NormalizedInvoiceLine]:
    lines = root.findall("cac:InvoiceLine", NS)
    if not lines:
        raise MissingRequiredFieldError("At least one invoice line is required.", field_path="Invoice/cac:InvoiceLine")
    return [_invoice_line(line) for line in lines]


def _invoice_line(line: ElementTree.Element) -> NormalizedInvoiceLine:
    quantity = line.find("cbc:InvoicedQuantity", NS)
    return NormalizedInvoiceLine(
        line_id=_required_text(line, "cbc:ID", "Invoice/cac:InvoiceLine/cbc:ID"),
        item_name=_optional_text(line, "cac:Item/cbc:Name"),
        description=_optional_text(line, "cac:Item/cbc:Description"),
        quantity=_decimal_from_element(quantity, "Invoice/cac:InvoiceLine/cbc:InvoicedQuantity")
        if quantity is not None
        else None,
        unit_code=quantity.attrib.get("unitCode") if quantity is not None else None,
        unit_price=_optional_decimal(line, "cac:Price/cbc:PriceAmount"),
        line_extension_amount=_required_decimal(
            line,
            "cbc:LineExtensionAmount",
            "Invoice/cac:InvoiceLine/cbc:LineExtensionAmount",
        ),
        allowance_charges=[_allowance_charge(item) for item in line.findall("cac:AllowanceCharge", NS)],
        tax_totals=_tax_totals(line.findall("cac:TaxTotal", NS), "Invoice/cac:InvoiceLine/cac:TaxTotal"),
    )


def _allowance_charge(element: ElementTree.Element) -> NormalizedAllowanceCharge:
    return NormalizedAllowanceCharge(
        charge_indicator=_bool_text(
            _required_text(
                element, "cbc:ChargeIndicator", "Invoice/cac:InvoiceLine/cac:AllowanceCharge/cbc:ChargeIndicator"
            )
        ),
        reason=_optional_text(element, "cbc:AllowanceChargeReason"),
        amount=_required_decimal(element, "cbc:Amount", "Invoice/cac:InvoiceLine/cac:AllowanceCharge/cbc:Amount"),
    )


def _references(root: ElementTree.Element) -> list[str]:
    references = []
    for path in (
        "cac:OrderReference/cbc:ID",
        "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID",
        "cac:DespatchDocumentReference/cbc:ID",
    ):
        references.extend(_texts(root, path))
    return references


def _required_text(element: ElementTree.Element, path: str, field_path: str | None = None) -> str:
    value = _optional_text(element, path)
    if value is None:
        raise MissingRequiredFieldError(f"Missing required field {field_path or path}.", field_path=field_path or path)
    return value


def _optional_text(element: ElementTree.Element | None, path: str) -> str | None:
    if element is None:
        return None
    child = element.find(path, NS)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _texts(element: ElementTree.Element, path: str) -> list[str]:
    return [child.text.strip() for child in element.findall(path, NS) if child.text and child.text.strip()]


def _first_text(element: ElementTree.Element, paths: tuple[str, ...]) -> str | None:
    for path in paths:
        value = _optional_text(element, path)
        if value is not None:
            return value
    return None


def _required_decimal(element: ElementTree.Element, path: str, field_path: str) -> Decimal:
    child = element.find(path, NS)
    if child is None:
        raise MissingRequiredFieldError(f"Missing required field {field_path}.", field_path=field_path)
    return _decimal_from_element(child, field_path)


def _optional_decimal(element: ElementTree.Element, path: str) -> Decimal | None:
    child = element.find(path, NS)
    if child is None or child.text is None or not child.text.strip():
        return None
    return _decimal_from_element(child, path)


def _decimal_from_element(element: ElementTree.Element, field_path: str) -> Decimal:
    text = (element.text or "").strip()
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise InvalidDecimalValueError(f"Invalid Decimal value at {field_path}.", field_path=field_path) from exc


def _bool_text(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise UnsupportedInvoiceStructureError("Invalid boolean value.", field_path="cbc:ChargeIndicator")


def _safe_log_extra(
    *,
    document: InvoiceDocument,
    started: float,
    result: str,
    category: str | None,
) -> dict[str, Any]:
    extra: dict[str, Any] = {
        "document_id": document.id,
        "document_type": document.document_type,
        "storage_key": document.storage_key,
        "duration_ms": round((perf_counter() - started) * 1000, 2),
        "result": result,
    }
    if category is not None:
        extra["category"] = category
    return extra


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag
