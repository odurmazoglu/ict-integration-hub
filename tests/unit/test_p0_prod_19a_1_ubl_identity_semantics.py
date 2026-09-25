"""P0-PROD-19A-1: UBL item identity namespaces stay separate.

- ``CommodityClassification`` is a classification, never a barcode, and never reaches an
  Odoo barcode lookup.
- ``ManufacturersItemIdentification`` is preserved in its own namespace.
- Source evidence stays backward compatible: evidence persisted before these fields
  existed round-trips byte-identically, so replay fingerprints are unchanged.
- Operating-expense / RESALE routing predicates keep their pre-19A-1 boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from app.application.expense_mapping import invoice_is_product_identifier_free
from app.application.expense_mapping.predicates import invoice_has_product_identifier
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, parse_ubl_invoice
from app.erp.models import Product
from app.matching import ProductMatchingEngine, ProductMatchStatus
from app.persistence.execution_source_invoice_reader import _invoice_from_data, _invoice_to_data

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ubl"
VITEL_VKN = "9250020961"
VITEL_SELLER_CODE = "1531012114"
MANAGEENGINE_SKU = "85710.1S1"

# Line keys written by the source-evidence serializer before P0-PROD-19A-1.
PRE_19A_LINE_KEYS = frozenset(
    {
        "line_number",
        "description",
        "seller_item_code",
        "buyer_item_code",
        "barcode",
        "quantity",
        "unit_code",
        "unit_price",
        "line_extension_amount",
        "discounts",
        "taxes",
    }
)


class RecordingProductRepository:
    def __init__(self, default_code_records: dict[str, Sequence[Product]] | None = None) -> None:
        self.default_code_records = default_code_records or {}
        self.calls: list[tuple[str, str]] = []

    def find_by_default_code(self, default_code: str, *, company_id: int | None = None) -> Sequence[Product]:
        del company_id
        self.calls.append(("default_code", default_code))
        return tuple(self.default_code_records.get(default_code, ()))

    def find_by_barcode(self, barcode: str, *, company_id: int | None = None) -> Sequence[Product]:
        del company_id
        self.calls.append(("barcode", barcode))
        return ()

    def find_by_ids(self, ids: Sequence[int]) -> Sequence[Product]:
        del ids
        return ()


class FakeProvider:
    def __init__(self, product_repository: RecordingProductRepository) -> None:
        self.product_repository = product_repository


def _vitel_invoice() -> InternalInvoice:
    return parse_ubl_invoice((FIXTURES / "vitel_manageengine_invoice.xml").read_bytes())


def _invoice(lines: list[InvoiceLine]) -> InternalInvoice:
    return InternalInvoice(
        header=Header(invoice_number="INV-19A", invoice_uuid="uuid-19a"),
        supplier=Party(tax_number=VITEL_VKN),
        customer=Party(),
        totals=MonetaryTotals(),
        lines=tuple(lines),
    )


# --- parser -------------------------------------------------------------------------


def test_vitel_line_keeps_each_identity_in_its_own_namespace() -> None:
    invoice = _vitel_invoice()
    line = invoice.lines[0]

    assert invoice.supplier.tax_number == VITEL_VKN
    assert line.description == MANAGEENGINE_SKU
    assert line.seller_item_code == VITEL_SELLER_CODE
    assert line.buyer_item_code is None
    assert line.manufacturer_item_code is None
    assert line.commodity_classification == "Subscription"


def test_commodity_classification_is_never_parsed_as_barcode() -> None:
    line = _vitel_invoice().lines[0]

    assert line.barcode is None
    assert line.barcode != "Subscription"


def test_standard_manufacturer_and_classification_identifiers_are_separate() -> None:
    line = _vitel_invoice().lines[1]

    assert line.barcode == "8690000000002"
    assert line.manufacturer_item_code == "85710.0MS5"
    assert line.commodity_classification == "License"
    assert line.seller_item_code == "SELLER-2"


def test_line_without_item_has_no_identity_fields() -> None:
    line = parse_ubl_invoice((FIXTURES / "minimal_invoice.xml").read_bytes()).lines[0]

    assert line.barcode is None
    assert line.manufacturer_item_code is None
    assert line.commodity_classification is None


def test_new_identity_fields_are_immutable_and_default_to_none() -> None:
    line = InvoiceLine(line_number="1")

    assert line.manufacturer_item_code is None
    assert line.commodity_classification is None
    with pytest.raises(FrozenInstanceError):
        line.commodity_classification = "x"  # type: ignore[misc]


# --- matcher (unchanged engine, corrected input) -----------------------------------


def test_vitel_line_never_performs_a_subscription_barcode_lookup() -> None:
    repository = RecordingProductRepository()
    invoice = _vitel_invoice()

    result = ProductMatchingEngine(FakeProvider(repository)).match_invoice(_invoice([invoice.lines[0]]), company_id=1)

    assert ("barcode", "Subscription") not in repository.calls
    assert all(kind != "barcode" for kind, _ in repository.calls)
    # 19A-1 does not yet resolve the ManageEngine SKU (that is 19A-2): the only lookup is the
    # pre-existing generic seller-code probe, and the Description is never used.
    assert repository.calls == [("default_code", VITEL_SELLER_CODE)]
    assert ("default_code", MANAGEENGINE_SKU) not in repository.calls
    line_result = result.line_results[0].result
    assert line_result.status is ProductMatchStatus.NOT_FOUND
    assert line_result.barcode is None


def test_classification_only_line_is_not_a_matchable_identifier() -> None:
    repository = RecordingProductRepository()
    line = InvoiceLine(line_number="1", commodity_classification="Subscription")

    result = ProductMatchingEngine(FakeProvider(repository)).match_invoice(_invoice([line]))

    assert repository.calls == []
    assert result.line_results[0].result.status is ProductMatchStatus.INVALID_INPUT


def test_manufacturer_item_code_is_not_yet_a_lookup_key() -> None:
    repository = RecordingProductRepository(
        {"85710.0MS5": [Product(id=392, name="ME", default_code="85710.0MS5", barcode=None, active=True)]}
    )
    line = InvoiceLine(line_number="1", manufacturer_item_code="85710.0MS5")

    result = ProductMatchingEngine(FakeProvider(repository)).match_invoice(_invoice([line]))

    assert repository.calls == []
    assert result.line_results[0].result.status is ProductMatchStatus.INVALID_INPUT


# --- source evidence compatibility --------------------------------------------------


def test_evidence_round_trip_preserves_new_identity_fields() -> None:
    invoice = _vitel_invoice()

    data = _invoice_to_data(invoice)
    restored = _invoice_from_data(data)

    assert data["lines"][0]["commodity_classification"] == "Subscription"
    assert "manufacturer_item_code" not in data["lines"][0]
    assert data["lines"][1]["manufacturer_item_code"] == "85710.0MS5"
    assert restored == invoice


def test_lines_without_new_fields_serialize_in_the_pre_19a_shape() -> None:
    invoice = _invoice([InvoiceLine(line_number="1", seller_item_code="S", barcode="869")])

    line_data = _invoice_to_data(invoice)["lines"][0]

    assert set(line_data) == PRE_19A_LINE_KEYS


def test_legacy_evidence_round_trips_byte_identically() -> None:
    legacy_invoice: dict[str, Any] = _invoice_to_data(
        _invoice([InvoiceLine(line_number="1", seller_item_code=VITEL_SELLER_CODE, barcode="Subscription")])
    )
    assert set(legacy_invoice["lines"][0]) == PRE_19A_LINE_KEYS

    restored = _invoice_from_data(legacy_invoice)

    # Legacy rows keep their stored barcode verbatim; nothing is re-derived on read.
    assert restored.lines[0].barcode == "Subscription"
    assert restored.lines[0].commodity_classification is None
    assert _invoice_to_data(restored) == legacy_invoice


# --- routing parity -----------------------------------------------------------------


def _pre_19a_line_has_identifier(line: InvoiceLine, standard_id: str | None, classification: str | None) -> bool:
    legacy_barcode = standard_id or classification
    return any(
        value is not None and value.strip() for value in (line.buyer_item_code, line.seller_item_code, legacy_barcode)
    )


@pytest.mark.parametrize(
    ("standard_id", "manufacturer", "classification", "seller"),
    [
        (None, None, None, None),
        (None, None, "Subscription", None),
        (None, None, "   ", None),
        ("869", None, None, None),
        ("869", None, "License", None),
        (None, "85710.1S1", None, None),
        (None, "85710.1S1", "Subscription", None),
        (None, None, "Subscription", VITEL_SELLER_CODE),
        (None, None, None, VITEL_SELLER_CODE),
    ],
)
def test_routing_predicates_match_pre_19a_boundary(
    standard_id: str | None,
    manufacturer: str | None,
    classification: str | None,
    seller: str | None,
) -> None:
    classification_text = classification.strip() if classification else None
    line = InvoiceLine(
        line_number="1",
        seller_item_code=seller,
        barcode=standard_id,
        manufacturer_item_code=manufacturer,
        commodity_classification=classification_text or None,
    )
    invoice = _invoice([line])
    expected_has_identifier = _pre_19a_line_has_identifier(line, standard_id, classification_text or None)

    assert invoice_has_product_identifier(invoice) is expected_has_identifier
    assert invoice_is_product_identifier_free(invoice) is (not expected_has_identifier)


def test_vitel_invoice_remains_product_shaped() -> None:
    invoice = _vitel_invoice()

    assert invoice_has_product_identifier(invoice) is True
    assert invoice_is_product_identifier_free(invoice) is False
