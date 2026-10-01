"""UBL-TR party tax identity: only typed VKN/TCKN (or PartyTaxScheme/CompanyID) is a tax number.

Regression for production invoice AAA2026000049722, whose customer party lists
PLAKA (vehicle plate) before its VKN, and for suppliers that list MERSISNO before
their VKN (the old parser took the first PartyIdentification/cbc:ID of either).
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

import pytest

from app.domain.invoice.exceptions import InvoiceDomainError
from app.domain.invoice.parser import NS, parse_ubl_invoice
from app.domain.invoice.party_tax_identity import (
    AmbiguousPartyTaxIdentifierError,
    MalformedPartyTaxIdentifierError,
    party_tax_identifier,
)
from app.erp.models import Company
from app.services.document_parser import InvalidPartyTaxIdentifierError, UblInvoiceParser
from app.services.uyumsoft_canonical_import import (
    IMPORT_STATUS_ACCEPTED,
    IMPORT_STATUS_NORMALIZATION_FAILED,
)
from tests.unit.test_uyumsoft_canonical_import import (
    RecordingImportUseCase,
    _importer,
    _invoice,
    _record,
    _success_result,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ubl"
AAA = FIXTURES / "vehicle_charging_plate_before_vkn_invoice.xml"
BUYER_VKN = "4651205941"
SUPPLIER_VKN = "0340303155"
PLATE = "34NZP916"
CUSTOMER_IDS_IN_FIXTURE = """      <cac:PartyIdentification>
        <cbc:ID schemeID="PLAKA">34NZP916</cbc:ID>
      </cac:PartyIdentification>
      <cac:PartyIdentification>
        <cbc:ID schemeID="ARACKIMLIKNO"/>
      </cac:PartyIdentification>
      <cac:PartyIdentification>
        <cbc:ID schemeID="VKN">4651205941</cbc:ID>
      </cac:PartyIdentification>
"""


def _ids(*pairs: tuple[str | None, str]) -> str:
    rendered = []
    for scheme, value in pairs:
        attribute = f' schemeID="{scheme}"' if scheme is not None else ""
        rendered.append(
            f"      <cac:PartyIdentification><cbc:ID{attribute}>{value}</cbc:ID></cac:PartyIdentification>\n"
        )
    return "".join(rendered)


def _invoice_with_customer(identifications: str, *, extra: str = "") -> bytes:
    text = AAA.read_text()
    assert CUSTOMER_IDS_IN_FIXTURE in text
    return text.replace(CUSTOMER_IDS_IN_FIXTURE, identifications + extra).encode()


def _party(xml: str) -> ElementTree.Element:
    return ElementTree.fromstring(f'<cac:Party xmlns:cac="{NS["cac"]}" xmlns:cbc="{NS["cbc"]}">{xml}</cac:Party>')


# --------------------------------------------------------------------------- 1. the AAA structure


def test_vehicle_charging_invoice_resolves_buyer_vkn_not_the_plate() -> None:
    invoice = parse_ubl_invoice(AAA.read_bytes())

    assert invoice.customer.tax_number == BUYER_VKN
    assert invoice.customer.tax_number != PLATE
    assert invoice.supplier.tax_number == SUPPLIER_VKN
    assert invoice.header.ettn == "01a0ded6-7942-70ab-b480-952b88bc16fd"


def test_services_document_parser_uses_the_same_selection() -> None:
    invoice = UblInvoiceParser().parse(AAA.read_bytes())

    assert (invoice.customer.tax_id, invoice.supplier.tax_id) == (BUYER_VKN, SUPPLIER_VKN)


def test_vehicle_charging_invoice_now_resolves_the_company_through_canonical_import() -> None:
    class ExactVknCompanies:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def find_by_tax_number(self, tax_number: str) -> tuple[Company, ...]:
            self.calls.append(tax_number)
            return (Company(id=1, name="ICT", tax_number=BUYER_VKN),) if tax_number == BUYER_VKN else ()

    companies = ExactVknCompanies()
    use_case = RecordingImportUseCase(_success_result())
    importer = _importer(use_case=use_case, content=AAA.read_bytes(), company_repository=companies)

    outcome = importer.import_invoice(_invoice(), persisted_record=_record())  # type: ignore[arg-type]

    assert companies.calls == [BUYER_VKN]
    assert outcome.status == IMPORT_STATUS_ACCEPTED
    assert outcome.company_id == 1
    assert use_case.commands[0].invoice.customer.tax_number == BUYER_VKN


# --------------------------------------------------------------------------- 2. unrelated identifiers in any order


@pytest.mark.parametrize(
    "pairs",
    [
        (("PLAKA", "34NZP916"), ("ARACKIMLIKNO", ""), ("VKN", BUYER_VKN)),
        (("VKN", BUYER_VKN), ("PLAKA", "34NZP916")),
        (("MERSISNO", "0465120594100013"), ("TICARETSICILNO", "123456"), ("VKN", BUYER_VKN), ("SUBENO", "1")),
        (("MERSISNO", "0465120594100013"), ("VKN", BUYER_VKN)),
        (("VKN", BUYER_VKN), ("TICARETSICILNO", "123456"), ("MERSISNO", "0465120594100013")),
    ],
)
def test_unrelated_party_identifications_are_never_the_tax_number(pairs: tuple[tuple[str, str], ...]) -> None:
    invoice = parse_ubl_invoice(_invoice_with_customer(_ids(*pairs)))

    assert invoice.customer.tax_number == BUYER_VKN


def test_supplier_mersis_before_vkn_resolves_the_vkn() -> None:
    """The production shape that stored a MERSIS number as two reviews' supplier tax number."""

    text = AAA.read_text().replace(
        '<cbc:ID schemeID="VKN">0340303155</cbc:ID>',
        '<cbc:ID schemeID="MERSISNO">0034030315500013</cbc:ID></cac:PartyIdentification>'
        '<cac:PartyIdentification><cbc:ID schemeID="VKN">0340303155</cbc:ID>',
    )

    assert parse_ubl_invoice(text.encode()).supplier.tax_number == SUPPLIER_VKN


@pytest.mark.parametrize(
    "pairs",
    [
        (("PLAKA", "34NZP916"),),
        ((None, "34NZP916"),),
        (("MERSISNO", "0465120594100013"), ("TICARETSICILNO", "123456")),
    ],
)
def test_party_without_any_tax_identifier_has_none_rather_than_another_id(
    pairs: tuple[tuple[str | None, str], ...],
) -> None:
    assert parse_ubl_invoice(_invoice_with_customer(_ids(*pairs))).customer.tax_number is None


# --------------------------------------------------------------------------- 3-5. explicit identity forms


@pytest.mark.parametrize("scheme", ["VKN", "vkn", " VKN "])
def test_explicit_vkn(scheme: str) -> None:
    assert party_tax_identifier(_party(_ids((scheme, f" {BUYER_VKN} "))), field_path="P") == BUYER_VKN


def test_explicit_tckn() -> None:
    party = _party(_ids(("PLAKA", "34ABC123"), ("TCKN", "12345678901")))

    assert party_tax_identifier(party, field_path="P") == "12345678901"


def test_party_tax_scheme_company_id_is_used_when_no_typed_identifier_exists() -> None:
    party = _party(
        _ids(("PLAKA", "34NZP916")) + "<cac:PartyTaxScheme><cbc:CompanyID>4651205941</cbc:CompanyID>"
        "<cac:TaxScheme><cbc:Name>Office</cbc:Name></cac:TaxScheme></cac:PartyTaxScheme>"
    )

    assert party_tax_identifier(party, field_path="P") == BUYER_VKN


def test_typed_identifier_takes_precedence_over_party_tax_scheme() -> None:
    party = _party(
        _ids(("VKN", BUYER_VKN)) + "<cac:PartyTaxScheme><cbc:CompanyID>12345678901</cbc:CompanyID></cac:PartyTaxScheme>"
    )

    assert party_tax_identifier(party, field_path="P") == BUYER_VKN


def test_legal_entity_company_id_is_not_a_tax_identifier() -> None:
    party = _party("<cac:PartyLegalEntity><cbc:CompanyID>123456</cbc:CompanyID></cac:PartyLegalEntity>")

    assert party_tax_identifier(party, field_path="P") is None


def test_repeated_identical_vkn_is_not_ambiguous() -> None:
    assert party_tax_identifier(_party(_ids(("VKN", BUYER_VKN), ("VKN", BUYER_VKN))), field_path="P") == BUYER_VKN


# --------------------------------------------------------------------------- 6-7. existing documents unchanged


def _old_tax_number(party: ElementTree.Element | None) -> str | None:
    """The previous selection: the first non-empty of these paths, schemeID ignored."""

    if party is None:
        return None
    for path in (
        "cac:PartyIdentification/cbc:ID",
        "cac:PartyTaxScheme/cbc:CompanyID",
        "cac:PartyLegalEntity/cbc:CompanyID",
    ):
        element = party.find(path, NS)
        if element is not None and element.text and element.text.strip():
            return element.text.strip()
    return None


@pytest.mark.parametrize("fixture", ["valid_invoice.xml", "minimal_invoice.xml", "vitel_manageengine_invoice.xml"])
def test_existing_fixtures_parse_exactly_as_before(fixture: str) -> None:
    content = (FIXTURES / fixture).read_bytes()
    root = ElementTree.fromstring(content)
    invoice = parse_ubl_invoice(content)

    assert invoice.supplier.tax_number == _old_tax_number(root.find("cac:AccountingSupplierParty/cac:Party", NS))
    assert invoice.customer.tax_number == _old_tax_number(root.find("cac:AccountingCustomerParty/cac:Party", NS))


def test_valid_fixture_tax_numbers_are_unchanged() -> None:
    invoice = parse_ubl_invoice((FIXTURES / "valid_invoice.xml").read_bytes())

    assert (invoice.supplier.tax_number, invoice.customer.tax_number) == ("1111111111", "2222222222")


# ------------------------------------------------------------- 8. ambiguous / malformed fail deterministically


@pytest.mark.parametrize(
    ("pairs", "error"),
    [
        ((("VKN", BUYER_VKN), ("VKN", "0340303155")), AmbiguousPartyTaxIdentifierError),
        ((("VKN", BUYER_VKN), ("TCKN", "12345678901")), AmbiguousPartyTaxIdentifierError),
        ((("VKN", "34NZP916"),), MalformedPartyTaxIdentifierError),
        ((("VKN", "465120594"),), MalformedPartyTaxIdentifierError),
        ((("TCKN", BUYER_VKN),), MalformedPartyTaxIdentifierError),
        ((("PLAKA", "34NZP916"), ("VKN", "46512059411")), MalformedPartyTaxIdentifierError),
    ],
)
def test_conflicting_or_malformed_tax_identifiers_fail_deterministically(
    pairs: tuple[tuple[str, str], ...], error: type[InvoiceDomainError]
) -> None:
    content = _invoice_with_customer(_ids(*pairs))

    with pytest.raises(error) as raised:
        parse_ubl_invoice(content)

    assert raised.value.field_path is not None
    assert raised.value.field_path.startswith("Invoice/cac:AccountingCustomerParty/cac:Party/")
    assert "34NZP916" not in str(raised.value)
    with pytest.raises(InvalidPartyTaxIdentifierError):
        UblInvoiceParser().parse(content)


def test_malformed_party_tax_scheme_company_id_fails() -> None:
    party = _party("<cac:PartyTaxScheme><cbc:CompanyID>ABC</cbc:CompanyID></cac:PartyTaxScheme>")

    with pytest.raises(MalformedPartyTaxIdentifierError):
        party_tax_identifier(party, field_path="P")


def test_conflicting_identifier_is_a_safe_normalization_failure_in_canonical_import() -> None:
    use_case = RecordingImportUseCase(_success_result())
    content = _invoice_with_customer(_ids(("VKN", BUYER_VKN), ("VKN", "0340303155")))
    importer = _importer(use_case=use_case, content=content)

    outcome = importer.import_invoice(_invoice(), persisted_record=_record())  # type: ignore[arg-type]

    assert outcome.status == IMPORT_STATUS_NORMALIZATION_FAILED
    assert outcome.safe_message == "Party has conflicting tax identifiers."
    assert use_case.commands == []
