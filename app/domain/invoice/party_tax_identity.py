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


def is_party_tax_identifier(value: str | None) -> bool:
    """Whether ``value`` has the shape of a VKN (10 digits) or TCKN (11 digits)."""

    return value is not None and value.isdigit() and len(value) in _TAX_REGISTRATION_LENGTHS


def is_valid_party_tax_identifier(value: str | None) -> bool:
    """Whether ``value`` is a well-formed VKN or TCKN *including* its check digits.

    Master-data quality check only (e.g. "is this stored partner VAT a real tax
    identifier or a legacy code?"). Never used to reject an incoming invoice.
    """

    if value is None or not value.isascii() or not is_party_tax_identifier(value):
        return False
    digits = [int(char) for char in value]
    return _vkn_check_digit_ok(digits) if len(digits) == 10 else _tckn_check_digits_ok(digits)


def _vkn_check_digit_ok(digits: list[int]) -> bool:
    total = 0
    for index, digit in enumerate(digits[:9]):
        shifted = (digit + 9 - index) % 10
        weighted = (shifted * 2 ** (9 - index)) % 9
        if shifted != 0 and weighted == 0:
            weighted = 9
        total += weighted
    return (10 - total % 10) % 10 == digits[9]


def _tckn_check_digits_ok(digits: list[int]) -> bool:
    if digits[0] == 0:
        return False
    odd = digits[0] + digits[2] + digits[4] + digits[6] + digits[8]
    even = digits[1] + digits[3] + digits[5] + digits[7]
    return (odd * 7 - even) % 10 == digits[9] and sum(digits[:10]) % 10 == digits[10]


def legacy_first_party_identifier(party: ElementTree.Element | None) -> str | None:
    """The identifier the pre-PR #201 parsers stored as a party's tax number.

    That rule took the first non-empty of ``PartyIdentification/cbc:ID`` (any
    ``schemeID``, only the first element), ``PartyTaxScheme/cbc:CompanyID`` and
    ``PartyLegalEntity/cbc:CompanyID``. It is **never** used to resolve a tax
    number; it exists only so a historical correction can prove that a persisted
    value is exactly what that defective rule produced from the same document.
    """

    if party is None:
        return None
    for path in (
        "cac:PartyIdentification/cbc:ID",
        "cac:PartyTaxScheme/cbc:CompanyID",
        "cac:PartyLegalEntity/cbc:CompanyID",
    ):
        element = party.find(path, NS)
        value = (element.text or "").strip() if element is not None else ""
        if value:
            return value
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
