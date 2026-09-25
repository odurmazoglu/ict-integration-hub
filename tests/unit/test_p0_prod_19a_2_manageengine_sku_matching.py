"""P0-PROD-19A-2: ManageEngine SKU deterministic matching with conflict safety.

- ``manufacturer_item_code`` -> exact ``product.product.default_code`` for every supplier.
- VİTEL (VKN 9250020961) only: a whole-field, strictly SKU-shaped ``cbc:Description``
  is an authoritative ManageEngine SKU. Never prose, never substrings, never ``cbc:Name``,
  never another supplier, never evidence of unknown provenance.
- All authoritative identities are evaluated; disagreement or ambiguity fails closed as
  ``MULTIPLE_MATCHES`` (existing persisted status, rollback-safe).
- Lines without an authoritative SKU keep the exact pre-19A-2 lookup chain.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from app.application.expense_mapping import invoice_is_product_identifier_free
from app.application.expense_mapping.predicates import invoice_has_product_identifier
from app.application.rules.deterministic import _product_review_reasons
from app.application.workflow import ManualReviewReasonCode
from app.domain.invoice import (
    DESCRIPTION_SOURCE_DESCRIPTION,
    DESCRIPTION_SOURCE_NAME,
    Header,
    InternalInvoice,
    InvoiceLine,
    MonetaryTotals,
    Party,
    parse_ubl_invoice,
)
from app.domain.invoice.source_profiles import VITEL_SUPPLIER_VKN, is_manageengine_sku, profile_manufacturer_sku
from app.erp.models import Product
from app.matching import ProductMatchingEngine, ProductMatchStatus
from app.persistence.execution_source_invoice_reader import _invoice_from_data, _invoice_to_data

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ubl"
OTHER_VKN = "1234567890"
ME_SKU = "85710.1S1"
VITEL_SELLER_CODE = "1531012114"


class RecordingProductRepository:
    def __init__(
        self,
        default_code_records: dict[str, Sequence[Product]] | None = None,
        barcode_records: dict[str, Sequence[Product]] | None = None,
    ) -> None:
        self.default_code_records = default_code_records or {}
        self.barcode_records = barcode_records or {}
        self.calls: list[tuple[str, str]] = []

    def find_by_default_code(self, default_code: str, *, company_id: int | None = None) -> Sequence[Product]:
        del company_id
        self.calls.append(("default_code", default_code))
        return tuple(self.default_code_records.get(default_code, ()))

    def find_by_barcode(self, barcode: str, *, company_id: int | None = None) -> Sequence[Product]:
        del company_id
        self.calls.append(("barcode", barcode))
        return tuple(self.barcode_records.get(barcode, ()))

    def find_by_ids(self, ids: Sequence[int]) -> Sequence[Product]:
        del ids
        return ()


class FakeProvider:
    def __init__(self, product_repository: RecordingProductRepository) -> None:
        self.product_repository = product_repository


def _product(product_id: int, default_code: str | None = None, *, active: bool = True) -> Product:
    return Product(id=product_id, name=f"P{product_id}", default_code=default_code, barcode=None, active=active)


def _invoice(lines: list[InvoiceLine], *, vkn: str | None = VITEL_SUPPLIER_VKN) -> InternalInvoice:
    return InternalInvoice(
        header=Header(invoice_number="INV-19A2", invoice_uuid="uuid-19a2"),
        supplier=Party(tax_number=vkn),
        customer=Party(),
        totals=MonetaryTotals(),
        lines=tuple(lines),
    )


def _vitel_line(description: str | None = ME_SKU, **kwargs: Any) -> InvoiceLine:
    kwargs.setdefault("seller_item_code", VITEL_SELLER_CODE)
    kwargs.setdefault("description_source", DESCRIPTION_SOURCE_DESCRIPTION if description is not None else None)
    return InvoiceLine(line_number="1", description=description, **kwargs)


def _match(invoice: InternalInvoice, repository: RecordingProductRepository) -> Any:
    return ProductMatchingEngine(FakeProvider(repository)).match_invoice(invoice).line_results[0].result


# --- source profile rule ------------------------------------------------------------


@pytest.mark.parametrize("sku", ["85710.1S1", "85710.1S3", "85710.0M6", "85710.0NS5", "67005.6MPMFA4", "6705.5MEE"])
def test_known_manageengine_skus_satisfy_the_strict_format(sku: str) -> None:
    assert is_manageengine_sku(sku)


@pytest.mark.parametrize(
    "value",
    [
        VITEL_SELLER_CODE,
        "ME-SI-U",
        "85710.",
        "85710.1",
        "857.1S1",
        "85710.1s1",
        "85710.S1",
        "85710 .1S1",
        "85710.1S1 extra",
        "Annual subscription 85710.1S1",
        "85710.1S1\n85710.1S3",
    ],
)
def test_non_sku_shapes_are_rejected(value: str) -> None:
    assert not is_manageengine_sku(value)
    assert profile_manufacturer_sku(_invoice([]), _vitel_line(value)) is None


def test_vitel_whole_field_description_is_a_manageengine_sku() -> None:
    assert profile_manufacturer_sku(_invoice([]), _vitel_line(ME_SKU)) == ME_SKU


def test_surrounding_whitespace_is_trimmed_for_description_and_vkn() -> None:
    invoice = _invoice([], vkn=f" {VITEL_SUPPLIER_VKN} ")

    assert profile_manufacturer_sku(invoice, _vitel_line(f"  {ME_SKU}\t")) == ME_SKU


@pytest.mark.parametrize("vkn", [OTHER_VKN, None, "", "92500209610", "925002096"])
def test_description_is_ignored_for_any_other_supplier(vkn: str | None) -> None:
    assert profile_manufacturer_sku(_invoice([], vkn=vkn), _vitel_line(ME_SKU)) is None


@pytest.mark.parametrize("source", [DESCRIPTION_SOURCE_NAME, None, "description", "Keyword"])
def test_only_proven_ubl_description_provenance_activates_the_profile(source: str | None) -> None:
    line = _vitel_line(ME_SKU, description_source=source)

    assert profile_manufacturer_sku(_invoice([]), line) is None


def test_missing_description_activates_nothing() -> None:
    assert profile_manufacturer_sku(_invoice([]), _vitel_line(None)) is None


# --- parser provenance --------------------------------------------------------------


def _single_line_ubl(item_xml: str) -> bytes:
    return f"""<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>VTL19A2</cbc:ID><cbc:UUID>19a20000-0000-4000-8000-000000000001</cbc:UUID>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyIdentification><cbc:ID schemeID="VKN">{VITEL_SUPPLIER_VKN}</cbc:ID></cac:PartyIdentification>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:InvoiceLine><cbc:ID>1</cbc:ID><cac:Item>{item_xml}</cac:Item></cac:InvoiceLine>
</Invoice>""".encode()


def test_parser_records_description_provenance() -> None:
    line = parse_ubl_invoice((FIXTURES / "vitel_manageengine_invoice.xml").read_bytes()).lines[0]

    assert line.description == ME_SKU
    assert line.description_source == DESCRIPTION_SOURCE_DESCRIPTION


def test_name_fallback_cannot_activate_the_vitel_profile() -> None:
    invoice = parse_ubl_invoice(_single_line_ubl(f"<cbc:Name>{ME_SKU}</cbc:Name>"))
    line = invoice.lines[0]
    repository = RecordingProductRepository({ME_SKU: [_product(392, ME_SKU)]})

    result = _match(invoice, repository)

    assert line.description == ME_SKU
    assert line.description_source == DESCRIPTION_SOURCE_NAME
    assert profile_manufacturer_sku(invoice, line) is None
    assert repository.calls == []
    assert result.status is ProductMatchStatus.INVALID_INPUT
    assert invoice_is_product_identifier_free(invoice) is True


def test_parser_without_description_or_name_has_no_provenance() -> None:
    line = parse_ubl_invoice(_single_line_ubl("<cbc:BrandName>X</cbc:BrandName>")).lines[0]

    assert line.description is None
    assert line.description_source is None


# --- matching -----------------------------------------------------------------------


def test_vitel_fixture_resolves_through_the_manageengine_sku() -> None:
    invoice = parse_ubl_invoice((FIXTURES / "vitel_manageengine_invoice.xml").read_bytes())
    repository = RecordingProductRepository({ME_SKU: [_product(392, ME_SKU)]})

    result = ProductMatchingEngine(FakeProvider(repository)).match_invoice(invoice).line_results[0].result

    assert result.status is ProductMatchStatus.MATCHED
    assert result.product_id == 392
    assert result.matched_by == "supplier_profile_sku"
    assert ("barcode", "Subscription") not in repository.calls
    assert repository.calls[:2] == [("default_code", VITEL_SELLER_CODE), ("default_code", ME_SKU)]


def test_the_product_id_comes_from_odoo_not_from_code() -> None:
    repository = RecordingProductRepository({ME_SKU: [_product(4242, ME_SKU)]})

    result = _match(_invoice([_vitel_line()]), repository)

    assert result.status is ProductMatchStatus.MATCHED
    assert result.product_id == 4242


def test_non_vitel_description_sku_is_never_looked_up() -> None:
    repository = RecordingProductRepository({ME_SKU: [_product(392, ME_SKU)]})

    result = _match(_invoice([_vitel_line(seller_item_code="S-1")], vkn=OTHER_VKN), repository)

    assert repository.calls == [("default_code", "S-1")]
    assert result.status is ProductMatchStatus.NOT_FOUND


@pytest.mark.parametrize("description", [f"Annual subscription {ME_SKU}", f"{ME_SKU} extra"])
def test_vitel_prose_description_is_never_looked_up(description: str) -> None:
    repository = RecordingProductRepository({ME_SKU: [_product(392, ME_SKU)]})

    result = _match(_invoice([_vitel_line(description)]), repository)

    assert repository.calls == [("default_code", VITEL_SELLER_CODE)]
    assert all(value != ME_SKU and value != description for _, value in repository.calls)
    assert result.status is ProductMatchStatus.NOT_FOUND


@pytest.mark.parametrize("vkn", [VITEL_SUPPLIER_VKN, OTHER_VKN])
def test_manufacturer_item_code_matches_default_code_for_any_supplier(vkn: str) -> None:
    repository = RecordingProductRepository({ME_SKU: [_product(392, ME_SKU)]})
    line = InvoiceLine(line_number="1", description="Some product", manufacturer_item_code=ME_SKU)

    result = _match(_invoice([line], vkn=vkn), repository)

    assert repository.calls == [("default_code", ME_SKU)]
    assert result.status is ProductMatchStatus.MATCHED
    assert result.product_id == 392
    assert result.matched_by == "manufacturer_item_code"


def test_manufacturer_sku_and_description_sku_converging_on_one_product_match() -> None:
    repository = RecordingProductRepository({ME_SKU: [_product(392, ME_SKU)]})

    result = _match(_invoice([_vitel_line(manufacturer_item_code=ME_SKU)]), repository)

    assert result.status is ProductMatchStatus.MATCHED
    assert result.product_id == 392
    assert result.matched_by == "manufacturer_item_code"
    assert "corroborated by supplier_profile_sku" in result.reason


def test_legacy_identity_converging_with_the_sku_matches() -> None:
    repository = RecordingProductRepository({"BUY-1": [_product(392, ME_SKU)], ME_SKU: [_product(392, ME_SKU)]})

    result = _match(_invoice([_vitel_line(buyer_item_code="BUY-1")]), repository)

    assert result.status is ProductMatchStatus.MATCHED
    assert result.product_id == 392
    assert result.matched_by == "default_code"
    assert "corroborated by supplier_profile_sku" in result.reason


def test_manufacturer_sku_and_description_sku_disagreeing_fail_closed() -> None:
    repository = RecordingProductRepository({ME_SKU: [_product(392, ME_SKU)], "85710.1S3": [_product(393)]})

    result = _match(_invoice([_vitel_line(manufacturer_item_code="85710.1S3")]), repository)

    assert result.status is ProductMatchStatus.MULTIPLE_MATCHES
    assert result.product_id is None
    assert result.matched_by is None
    assert result.candidate_count == 2
    assert result.reason.startswith("Conflicting product identities")
    assert "manufacturer_item_code -> product 393" in result.reason
    assert "supplier_profile_sku -> product 392" in result.reason


@pytest.mark.parametrize(
    ("line_kwargs", "repository_kwargs"),
    [
        ({"buyer_item_code": "BUY-1"}, {"default_code_records": {"BUY-1": [_product(10)]}}),
        ({"barcode": "869"}, {"barcode_records": {"869": [_product(10)]}}),
        ({"seller_item_code": "SUP-1"}, {"default_code_records": {"SUP-1": [_product(10)]}}),
    ],
    ids=["buyer", "barcode", "seller"],
)
def test_legacy_identity_disagreeing_with_the_sku_fails_closed(
    line_kwargs: dict[str, str],
    repository_kwargs: dict[str, dict[str, list[Product]]],
) -> None:
    repository = RecordingProductRepository(**repository_kwargs)
    repository.default_code_records[ME_SKU] = [_product(392, ME_SKU)]

    result = _match(_invoice([_vitel_line(**line_kwargs)]), repository)

    assert result.status is ProductMatchStatus.MULTIPLE_MATCHES
    assert result.product_id is None
    assert result.reason.startswith("Conflicting product identities")


def test_legacy_ambiguity_is_not_overridden_by_a_unique_sku() -> None:
    repository = RecordingProductRepository({"BUY-1": [_product(10), _product(11)], ME_SKU: [_product(392, ME_SKU)]})

    result = _match(_invoice([_vitel_line(buyer_item_code="BUY-1")]), repository)

    assert result.status is ProductMatchStatus.MULTIPLE_MATCHES
    assert result.product_id is None
    assert result.reason == "Multiple active product candidates found by default_code."


def test_duplicate_default_code_for_the_sku_is_ambiguous() -> None:
    repository = RecordingProductRepository({ME_SKU: [_product(392, ME_SKU), _product(393, ME_SKU)]})

    result = _match(_invoice([_vitel_line()]), repository)

    assert result.status is ProductMatchStatus.MULTIPLE_MATCHES
    assert result.candidate_count == 2
    assert result.reason == "Multiple active product candidates found by supplier_profile_sku."


def test_archived_sku_product_is_ignored_like_every_other_identity() -> None:
    repository = RecordingProductRepository({ME_SKU: [_product(392, ME_SKU, active=False)]})

    result = _match(_invoice([_vitel_line()]), repository)

    assert result.status is ProductMatchStatus.NOT_FOUND


def test_archived_duplicate_does_not_make_the_sku_ambiguous() -> None:
    repository = RecordingProductRepository({ME_SKU: [_product(392, ME_SKU), _product(900, ME_SKU, active=False)]})

    result = _match(_invoice([_vitel_line()]), repository)

    assert result.status is ProductMatchStatus.MATCHED
    assert result.product_id == 392


# --- manual review mapping ----------------------------------------------------------


def _review_codes(invoice: InternalInvoice, repository: RecordingProductRepository) -> list[ManualReviewReasonCode]:
    result = ProductMatchingEngine(FakeProvider(repository)).match_invoice(invoice)
    return [reason.code for reason in _product_review_reasons(invoice, result)]


def test_no_match_remains_product_not_found() -> None:
    invoice = _invoice([_vitel_line()])

    assert _review_codes(invoice, RecordingProductRepository()) == [ManualReviewReasonCode.PRODUCT_NOT_FOUND]


def test_conflict_surfaces_as_product_ambiguous() -> None:
    invoice = _invoice([_vitel_line(manufacturer_item_code="85710.1S3")])
    repository = RecordingProductRepository({ME_SKU: [_product(392)], "85710.1S3": [_product(393)]})

    assert _review_codes(invoice, repository) == [ManualReviewReasonCode.PRODUCT_AMBIGUOUS]


def test_match_produces_no_product_review_reason() -> None:
    invoice = _invoice([_vitel_line()])

    assert _review_codes(invoice, RecordingProductRepository({ME_SKU: [_product(392)]})) == []


# --- routing predicates stay consistent with lookups --------------------------------


def test_vitel_sku_only_line_is_product_shaped() -> None:
    invoice = _invoice([_vitel_line(seller_item_code=None)])

    assert invoice_has_product_identifier(invoice) is True
    assert invoice_is_product_identifier_free(invoice) is False


@pytest.mark.parametrize(
    ("vkn", "description", "source"),
    [
        (OTHER_VKN, ME_SKU, DESCRIPTION_SOURCE_DESCRIPTION),
        (VITEL_SUPPLIER_VKN, f"Annual subscription {ME_SKU}", DESCRIPTION_SOURCE_DESCRIPTION),
        (VITEL_SUPPLIER_VKN, ME_SKU, DESCRIPTION_SOURCE_NAME),
        (VITEL_SUPPLIER_VKN, ME_SKU, None),
    ],
    ids=["other-supplier", "prose", "name-fallback", "unknown-provenance"],
)
def test_description_outside_the_profile_leaves_the_line_identifier_free(
    vkn: str, description: str, source: str | None
) -> None:
    line = InvoiceLine(line_number="1", description=description, description_source=source)
    invoice = _invoice([line], vkn=vkn)
    repository = RecordingProductRepository({ME_SKU: [_product(392)]})

    assert invoice_is_product_identifier_free(invoice) is True
    assert _match(invoice, repository).status is ProductMatchStatus.INVALID_INPUT
    assert repository.calls == []


# --- evidence compatibility ---------------------------------------------------------


def test_description_provenance_round_trips_through_evidence() -> None:
    invoice = parse_ubl_invoice((FIXTURES / "vitel_manageengine_invoice.xml").read_bytes())

    data = _invoice_to_data(invoice)

    assert data["lines"][0]["description_source"] == DESCRIPTION_SOURCE_DESCRIPTION
    assert _invoice_from_data(data) == invoice


def test_legacy_evidence_without_provenance_replays_byte_identically_and_stays_inert() -> None:
    legacy = _invoice_to_data(_invoice([InvoiceLine(line_number="1", description=ME_SKU, seller_item_code="S")]))
    assert "description_source" not in legacy["lines"][0]

    restored = _invoice_from_data(legacy)
    repository = RecordingProductRepository({ME_SKU: [_product(392)]})

    assert restored.lines[0].description_source is None
    assert _invoice_to_data(restored) == legacy
    # Unknown provenance never activates the profile: an existing review re-evaluated from
    # legacy evidence performs exactly the pre-19A-2 lookups.
    assert _match(restored, repository).status is ProductMatchStatus.NOT_FOUND
    assert repository.calls == [("default_code", "S")]
