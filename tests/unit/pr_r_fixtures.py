"""Synthetic PR R fixtures. Every identifier, tax number and name here is fictitious."""

from __future__ import annotations

from collections.abc import Sequence

COMPANY_ID = 7
#: Synthetic tax numbers: ICT (return buyer / original seller) and the returning customer.
COMPANY_VKN = "9000000002"
CUSTOMER_VKN = "9000000001"
RETURN_UUID = "5a5a0000-0000-4000-8000-0000000000a1"
REFERENCE_NUMBER = "TST2026000000001"
SECOND_REFERENCE_NUMBER = "TST2026000000002"
REFERENCE_DATE = "2026-09-10"
RETURN_DATE = "2026-09-15"
ORIGINAL_ETTN = "6b6b0000-0000-4000-8000-000000000001"
SECOND_ORIGINAL_ETTN = "6b6b0000-0000-4000-8000-000000000002"

Reference = tuple[str | None, str | None, str | None]


def return_ubl(
    *,
    references: Sequence[Reference] = ((REFERENCE_NUMBER, REFERENCE_DATE, "IADE"),),
    invoice_type: str = "IADE",
    currency: str = "TRY",
    supplier_vkn: str = CUSTOMER_VKN,
    customer_vkn: str = COMPANY_VKN,
    uuid: str = RETURN_UUID,
    issue_date: str = RETURN_DATE,
    note: str | None = None,
    line_text: str = "Returned goods",
    additional_reference_id: str | None = None,
) -> bytes:
    billing = "".join(_billing_reference(*reference) for reference in references)
    note_xml = f"<cbc:Note>{note}</cbc:Note>" if note else ""
    additional = (
        f"<cac:AdditionalDocumentReference><cbc:ID>{additional_reference_id}</cbc:ID>"
        f"<cbc:IssueDate>{issue_date}</cbc:IssueDate></cac:AdditionalDocumentReference>"
        if additional_reference_id
        else ""
    )
    return f"""<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ProfileID>TEMELFATURA</cbc:ProfileID>
  <cbc:ID>RET2026000000001</cbc:ID><cbc:UUID>{uuid}</cbc:UUID>
  <cbc:IssueDate>{issue_date}</cbc:IssueDate>
  <cbc:InvoiceTypeCode>{invoice_type}</cbc:InvoiceTypeCode>
  {note_xml}
  <cbc:DocumentCurrencyCode>{currency}</cbc:DocumentCurrencyCode>
  {billing}
  {additional}
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyIdentification><cbc:ID schemeID="VKN">{supplier_vkn}</cbc:ID></cac:PartyIdentification>
    <cac:PartyName><cbc:Name>SYNTHETIC RETURNING CUSTOMER</cbc:Name></cac:PartyName>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party>
    <cac:PartyIdentification><cbc:ID schemeID="VKN">{customer_vkn}</cbc:ID></cac:PartyIdentification>
    <cac:PartyName><cbc:Name>SYNTHETIC COMPANY</cbc:Name></cac:PartyName>
  </cac:Party></cac:AccountingCustomerParty>
  <cac:InvoiceLine>
    <cbc:ID>1</cbc:ID>
    <cbc:InvoicedQuantity unitCode="C62">1</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="{currency}">10.00</cbc:LineExtensionAmount>
    <cac:Item><cbc:Name>{line_text}</cbc:Name></cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="{currency}">10.00</cbc:PriceAmount></cac:Price>
  </cac:InvoiceLine>
</Invoice>""".encode()


def _billing_reference(number: str | None, issue_date: str | None, type_code: str | None) -> str:
    parts = [
        f"<cbc:ID>{number}</cbc:ID>" if number is not None else "",
        f"<cbc:IssueDate>{issue_date}</cbc:IssueDate>" if issue_date is not None else "",
        f"<cbc:DocumentTypeCode>{type_code}</cbc:DocumentTypeCode>" if type_code is not None else "",
    ]
    inner = "".join(parts)
    return (
        "<cac:BillingReference><cac:InvoiceDocumentReference>"
        f"{inner}"
        "</cac:InvoiceDocumentReference></cac:BillingReference>"
    )
