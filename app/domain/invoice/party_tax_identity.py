"""Tax identity of a UBL-TR party (supplier or customer).

In Turkish e-Fatura UBL, a party's tax identity is a ``cac:PartyIdentification/cbc:ID``
whose ``schemeID`` is ``VKN`` (10-digit tax number) or ``TCKN`` (11-digit national id).
The same element also carries unrelated identifiers -- ``MERSISNO``,
``TICARETSICILNO``, ``SUBENO``, ``PLAKA`` (vehicle plate), ``ARACKIMLIKNO`` ... -- in
any order, so the *first* ``PartyIdentification/cbc:ID`` is not a tax identifier.

Selection:

1. typed ``VKN``/``TCKN`` identifiers (an empty element is absent);
2. only if there is none, the generic UBL tax registration
   ``cac:PartyTaxScheme/cbc:CompanyID``;
3. otherwise ``None``.

Malformed or conflicting tax identifiers fail deterministically instead of
falling through to another identifier. Untyped/other ``PartyIdentification`` ids
and ``cac:PartyLegalEntity/cbc:CompanyID`` (legal registration) are never used.
"""

from __future__ import annotations

from xml.etree import ElementTree

from app.domain.invoice.exceptions import InvoiceDomainError

NS = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}
#: Typed tax-identifier schemes and their exact digit counts.
TAX_IDENTIFIER_SCHEMES = {"VKN": 10, "TCKN": 11}
_TAX_REGISTRATION_LENGTHS = frozenset(TAX_IDENTIFIER_SCHEMES.values())


class MalformedPartyTaxIdentifierError(InvoiceDomainError):
    safe_message = "Party tax identifier is malformed."


class AmbiguousPartyTaxIdentifierError(InvoiceDomainError):
    safe_message = "Party has conflicting tax identifiers."


def party_tax_identifier(party: ElementTree.Element | None, *, field_path: str) -> str | None:
    if party is None:
        return None
    typed = _typed_tax_identifiers(party, field_path=field_path)
    if typed:
        return _single(typed, field_path=f"{field_path}/cac:PartyIdentification/cbc:ID")
    registrations = _tax_registrations(party, field_path=field_path)
    if registrations:
        return _single(registrations, field_path=f"{field_path}/cac:PartyTaxScheme/cbc:CompanyID")
    return None


def _typed_tax_identifiers(party: ElementTree.Element, *, field_path: str) -> list[str]:
    values: list[str] = []
    for element in party.findall("cac:PartyIdentification/cbc:ID", NS):
        scheme = (element.attrib.get("schemeID") or "").strip().upper()
        value = (element.text or "").strip()
        if scheme not in TAX_IDENTIFIER_SCHEMES or not value:
            continue
        if not (value.isdigit() and len(value) == TAX_IDENTIFIER_SCHEMES[scheme]):
            raise MalformedPartyTaxIdentifierError(
                f"{scheme} must be exactly {TAX_IDENTIFIER_SCHEMES[scheme]} digits.",
                field_path=f"{field_path}/cac:PartyIdentification/cbc:ID[@schemeID='{scheme}']",
            )
        values.append(value)
    return values


def _tax_registrations(party: ElementTree.Element, *, field_path: str) -> list[str]:
    values: list[str] = []
    for element in party.findall("cac:PartyTaxScheme/cbc:CompanyID", NS):
        value = (element.text or "").strip()
        if not value:
            continue
        if not (value.isdigit() and len(value) in _TAX_REGISTRATION_LENGTHS):
            raise MalformedPartyTaxIdentifierError(
                "PartyTaxScheme CompanyID must be a 10-digit VKN or an 11-digit TCKN.",
                field_path=f"{field_path}/cac:PartyTaxScheme/cbc:CompanyID",
            )
        values.append(value)
    return values


def _single(values: list[str], *, field_path: str) -> str:
    distinct = sorted(set(values))
    if len(distinct) > 1:
        raise AmbiguousPartyTaxIdentifierError(AmbiguousPartyTaxIdentifierError.safe_message, field_path=field_path)
    return distinct[0]
