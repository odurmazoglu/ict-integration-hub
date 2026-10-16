"""Synthetic UBL-TR fixtures for PR T W1 (withholding / source-tax guard).

Every party, number and UUID here is synthetic. ``synthetic_ubl`` builds documents the
canonical parser accepts, so the same bytes drive both the source-tax extractor and
import; the ``*_SHAPE`` helpers mirror the tax shapes of today's 62 production invoices
(every TaxSubtotal TaxTypeCode 0015, one tax per line, no WithholdingTaxTotal).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

SYNTHETIC_UUID = "7e570000-0000-4000-8000-0000000000a1"
SUPPLIER_VKN = "1000000146"
BUYER_VKN = "2000000010"


@dataclass(frozen=True, slots=True)
class TaxSpec:
    code: str | None = "0015"
    name: str = "KDV"
    percent: str | None = "20"
    amount: str = "20.00"
    exemption_code: str | None = None


@dataclass(frozen=True, slots=True)
class LineSpec:
    taxes: tuple[TaxSpec, ...] = (TaxSpec(),)
    withholding: bool = False
    name: str = "Synthetic service"


@dataclass(frozen=True, slots=True)
class DocumentSpec:
    uuid: str = SYNTHETIC_UUID
    invoice_type: str | None = "SATIS"
    profile: str = "TICARIFATURA"
    lines: tuple[LineSpec, ...] = (LineSpec(),)
    invoice_taxes: tuple[TaxSpec, ...] | None = None
    invoice_withholding: bool = False
    #: Namespace prefix used for WithholdingTaxTotal elements (``ext`` = a foreign namespace).
    withholding_prefix: str = "cac"
    prolog: str = ""
    extra: list[str] = field(default_factory=list)


def synthetic_ubl(spec: DocumentSpec | None = None, **overrides: object) -> bytes:
    spec = spec or DocumentSpec(**overrides)  # type: ignore[arg-type]
    invoice_taxes = (
        spec.invoice_taxes
        if spec.invoice_taxes is not None
        else tuple(tax for line in spec.lines for tax in line.taxes)
    )
    type_xml = f"<cbc:InvoiceTypeCode>{spec.invoice_type}</cbc:InvoiceTypeCode>" if spec.invoice_type else ""
    lines_xml = "".join(_line(index, line, spec.withholding_prefix) for index, line in enumerate(spec.lines, 1))
    return f"""{spec.prolog}<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
         xmlns:ext="urn:example:synthetic:extension">
  <cbc:ProfileID>{spec.profile}</cbc:ProfileID>
  <cbc:ID>SYN2026W10000001</cbc:ID>
  <cbc:UUID>{spec.uuid}</cbc:UUID>
  <cbc:IssueDate>2026-10-01</cbc:IssueDate>
  {type_xml}
  <cbc:DocumentCurrencyCode>TRY</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyIdentification><cbc:ID schemeID="VKN">{SUPPLIER_VKN}</cbc:ID></cac:PartyIdentification>
    <cac:PartyName><cbc:Name>SYNTHETIC SUPPLIER</cbc:Name></cac:PartyName>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party>
    <cac:PartyIdentification><cbc:ID schemeID="VKN">{BUYER_VKN}</cbc:ID></cac:PartyIdentification>
    <cac:PartyName><cbc:Name>SYNTHETIC BUYER</cbc:Name></cac:PartyName>
  </cac:Party></cac:AccountingCustomerParty>
  {"".join(spec.extra)}
  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="TRY">20.00</cbc:TaxAmount>
    {"".join(_subtotal(tax) for tax in invoice_taxes)}
  </cac:TaxTotal>
  {_withholding(spec.withholding_prefix) if spec.invoice_withholding else ""}
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="TRY">100.00</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="TRY">100.00</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="TRY">120.00</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="TRY">120.00</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  {lines_xml}
</Invoice>""".encode()


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _line(index: int, line: LineSpec, withholding_prefix: str) -> str:
    return f"""
  <cac:InvoiceLine>
    <cbc:ID>{index}</cbc:ID>
    <cbc:InvoicedQuantity unitCode="C62">1</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="TRY">100.00</cbc:LineExtensionAmount>
    <cac:TaxTotal>
      <cbc:TaxAmount currencyID="TRY">20.00</cbc:TaxAmount>
      {"".join(_subtotal(tax) for tax in line.taxes)}
    </cac:TaxTotal>
    {_withholding(withholding_prefix) if line.withholding else ""}
    <cac:Item><cbc:Name>{line.name}</cbc:Name></cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="TRY">100.00</cbc:PriceAmount></cac:Price>
  </cac:InvoiceLine>"""


def _subtotal(tax: TaxSpec) -> str:
    percent = f"<cbc:Percent>{tax.percent}</cbc:Percent>" if tax.percent is not None else ""
    exemption = (
        f"<cbc:TaxExemptionReasonCode>{tax.exemption_code}</cbc:TaxExemptionReasonCode>" if tax.exemption_code else ""
    )
    code = f"<cbc:TaxTypeCode>{tax.code}</cbc:TaxTypeCode>" if tax.code is not None else ""
    return f"""
      <cac:TaxSubtotal>
        <cbc:TaxableAmount currencyID="TRY">100.00</cbc:TaxableAmount>
        <cbc:TaxAmount currencyID="TRY">{tax.amount}</cbc:TaxAmount>
        {percent}
        <cac:TaxCategory>{exemption}
          <cac:TaxScheme><cbc:Name>{tax.name}</cbc:Name>{code}</cac:TaxScheme>
        </cac:TaxCategory>
      </cac:TaxSubtotal>"""


def _withholding(prefix: str) -> str:
    return f"""
  <{prefix}:WithholdingTaxTotal>
    <cbc:TaxAmount currencyID="TRY">10.00</cbc:TaxAmount>
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="TRY">20.00</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="TRY">10.00</cbc:TaxAmount>
      <cbc:Percent>50</cbc:Percent>
      <cac:TaxCategory><cac:TaxScheme>
        <cbc:Name>KDV TEVKIFAT</cbc:Name><cbc:TaxTypeCode>650</cbc:TaxTypeCode>
      </cac:TaxScheme></cac:TaxCategory>
    </cac:TaxSubtotal>
  </{prefix}:WithholdingTaxTotal>"""


#: The tax shapes of today's 62 production invoices (synthetic values).
PRODUCTION_COMPATIBILITY_SHAPES: dict[str, DocumentSpec] = {
    "commercial_20_percent": DocumentSpec(),
    "basic_profile_10_percent": DocumentSpec(
        profile="TEMELFATURA", lines=(LineSpec(taxes=(TaxSpec(percent="10", amount="10.00"),)),)
    ),
    "ev_charging_long_vat_name": DocumentSpec(
        profile="ENERJI",
        lines=(LineSpec(taxes=(TaxSpec(name="GERÇEK USULDE KATMA DEĞER VERGİSİ"),)),),
    ),
    "kdv_percent_label": DocumentSpec(lines=(LineSpec(taxes=(TaxSpec(name="KDV %1", percent="1", amount="1.00"),)),)),
    "exempt_335_without_percent": DocumentSpec(
        invoice_type="ISTISNA",
        lines=(LineSpec(taxes=(TaxSpec(percent=None, amount="0.00", exemption_code="335"),)),),
    ),
    "zero_rate_exemption_209": DocumentSpec(
        invoice_type="ISTISNA",
        lines=(LineSpec(taxes=(TaxSpec(percent="0", amount="0.00", exemption_code="209"),)),),
    ),
    "mixed_rate_eight_lines": DocumentSpec(
        lines=tuple(
            LineSpec(taxes=(TaxSpec(percent=percent, amount="1.00", exemption_code="351" if percent == "0" else None),))
            for percent in ("20", "10", "1", "20", "10", "0", "20", "1")
        )
    ),
    "return_invoice": DocumentSpec(invoice_type="IADE"),
    "no_invoice_type_code": DocumentSpec(invoice_type=None),
}
