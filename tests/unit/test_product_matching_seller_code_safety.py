"""Product matching safety: a seller code is not a global ICT product identity.

- ``seller_item_code -> product.default_code`` is advisory only: it never produces
  ``MATCHED``, never conflicts with and never makes ambiguous a deterministic match.
- ``(resolved supplier, seller_item_code) -> product.supplierinfo -> variant`` is the
  supplier-specific identity and the primary ``matched_by`` when it matches.
- Buyer code / barcode / manufacturer SKUs keep their deterministic semantics, and
  genuine ambiguity or disagreement between them still fails closed.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest

from app.application.rules.deterministic import _product_review_reasons
from app.application.workflow import ManualReviewReasonCode
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party
from app.erp.models import Product, ProductVariant, SupplierProductCode
from app.matching import PartnerMatchResult, PartnerMatchStatus, ProductMatchingEngine, ProductMatchStatus

COMPANY_ID = 1
SUPPLIER_A = 450
SUPPLIER_B = 451
SELLER_CODE = "CFQ7TTC0LH18:0001"
SELLER_CODE_2 = "CFQ7TTC0LDPB:0001"


class ProductRepository:
    def __init__(
        self,
        default_codes: dict[str, Sequence[Product]] | None = None,
        barcodes: dict[str, Sequence[Product]] | None = None,
    ) -> None:
        self.default_codes = default_codes or {}
        self.barcodes = barcodes or {}
        self.calls: list[tuple[str, str]] = []

    def find_by_default_code(self, default_code: str, *, company_id: int | None = None) -> Sequence[Product]:
        del company_id
        self.calls.append(("default_code", default_code))
        return tuple(self.default_codes.get(default_code, ()))

    def find_by_barcode(self, barcode: str, *, company_id: int | None = None) -> Sequence[Product]:
        del company_id
        self.calls.append(("barcode", barcode))
        return tuple(self.barcodes.get(barcode, ()))

    def find_by_ids(self, ids: Sequence[int]) -> Sequence[Product]:
        del ids
        return ()


class SupplierRepository:
    """``(partner, seller code) -> supplierinfo rows``; ``template -> variants``."""

    def __init__(
        self,
        rows: dict[tuple[int, str], list[SupplierProductCode]] | None = None,
        variants: dict[int, list[ProductVariant]] | None = None,
    ) -> None:
        self.rows = rows or {}
        self.variants = variants or {}

    def find_supplier_product_codes(
        self, *, partner_id: int, product_code: str, company_id: int, limit: int
    ) -> Sequence[SupplierProductCode]:
        del company_id
        return tuple(self.rows.get((partner_id, product_code), ())[:limit])

    def find_template_variants(
        self, *, product_tmpl_id: int, company_id: int, variant_id: int | None, limit: int
    ) -> Sequence[ProductVariant]:
        del company_id
        candidates = self.variants.get(product_tmpl_id, [])
        if variant_id is not None:
            candidates = [variant for variant in candidates if variant.id == variant_id]
        return tuple(candidates[:limit])


class Provider:
    def __init__(self, product_repository: ProductRepository) -> None:
        self.product_repository = product_repository


def _product(product_id: int, default_code: str | None = None) -> Product:
    return Product(id=product_id, name=f"P{product_id}", default_code=default_code, barcode=None, active=True)


def _variant(variant_id: int, template_id: int) -> ProductVariant:
    return ProductVariant(id=variant_id, product_tmpl_id=template_id, active=True, company_id=None)


def _row(partner_id: int, code: str, template_id: int, product_id: int | None, row_id: int = 1) -> SupplierProductCode:
    return SupplierProductCode(
        id=row_id,
        partner_id=partner_id,
        product_code=code,
        product_tmpl_id=template_id,
        product_id=product_id,
        company_id=COMPANY_ID,
    )


def _matched(partner_id: int) -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MATCHED,
        partner_id=partner_id,
        matched_by="tax_number",
        reason="test",
        candidate_count=1,
        confidence=Decimal("1.00"),
    )


def _invoice(*lines: InvoiceLine) -> InternalInvoice:
    return InternalInvoice(
        header=Header(invoice_number="INV-SAFETY", invoice_uuid="uuid-safety"),
        supplier=Party(tax_number="6090213253"),
        customer=Party(),
        totals=MonetaryTotals(),
        lines=lines,
    )


def _line(number: str = "1", **kwargs: Any) -> InvoiceLine:
    return InvoiceLine(line_number=number, **kwargs)


def _match_all(
    invoice: InternalInvoice,
    *,
    products: ProductRepository | None = None,
    supplier: SupplierRepository | None = None,
    partner_id: int = SUPPLIER_A,
) -> list[Any]:
    engine = ProductMatchingEngine(
        Provider(products or ProductRepository()),
        supplier_product_repository=supplier or SupplierRepository(),
    )
    result = engine.match_invoice(invoice, company_id=COMPANY_ID, partner_match=_matched(partner_id))
    return [line.result for line in result.line_results]


def _match(line: InvoiceLine, **kwargs: Any) -> Any:
    return _match_all(_invoice(line), **kwargs)[0]


# --- seller code is never a global identity -----------------------------------------


def test_seller_code_equal_to_an_unrelated_default_code_is_not_matched() -> None:
    products = ProductRepository({SELLER_CODE: [_product(393, SELLER_CODE)]})

    result = _match(_line(seller_item_code=SELLER_CODE), products=products)

    assert result.status is ProductMatchStatus.NOT_FOUND
    assert (result.product_id, result.matched_by, result.confidence) == (None, None, None)
    assert "Seller item code equals the default_code of 1 active product(s)" in result.reason
    assert "supplier-specific mapping is required" in result.reason


def test_advisory_only_line_surfaces_as_product_not_found_review_reason() -> None:
    invoice = _invoice(_line(seller_item_code=SELLER_CODE))
    engine = ProductMatchingEngine(Provider(ProductRepository({SELLER_CODE: [_product(393, SELLER_CODE)]})))

    result = engine.match_invoice(invoice, company_id=COMPANY_ID, partner_match=_matched(SUPPLIER_A))

    assert [reason.code for reason in _product_review_reasons(invoice, result)] == [
        ManualReviewReasonCode.PRODUCT_NOT_FOUND
    ]


def test_ambiguous_global_seller_code_hit_is_not_ambiguity() -> None:
    products = ProductRepository({SELLER_CODE: [_product(1, SELLER_CODE), _product(2, SELLER_CODE)]})

    result = _match(_line(seller_item_code=SELLER_CODE), products=products)

    assert result.status is ProductMatchStatus.NOT_FOUND
    assert result.candidate_count == 0


def test_advisory_probe_keeps_the_previous_call_budget() -> None:
    products = ProductRepository({"BUY-1": [_product(10, "BUY-1")], SELLER_CODE: [_product(393, SELLER_CODE)]})

    result = _match(_line(buyer_item_code="BUY-1", seller_item_code=SELLER_CODE), products=products)

    assert result.status is ProductMatchStatus.MATCHED
    assert result.product_id == 10
    assert products.calls == [("default_code", "BUY-1")]


# --- supplier-specific mapping ------------------------------------------------------


def test_supplier_mapping_matches_the_exact_intended_variant() -> None:
    supplier = SupplierRepository(
        {(SUPPLIER_A, SELLER_CODE): [_row(SUPPLIER_A, SELLER_CODE, 70, 702)]},
        {70: [_variant(701, 70), _variant(702, 70), _variant(703, 70)]},
    )

    result = _match(_line(seller_item_code=SELLER_CODE), supplier=supplier)

    assert result.status is ProductMatchStatus.MATCHED
    assert result.product_id == 702
    assert result.matched_by == "supplier_product_code"
    assert result.confidence == Decimal("1.00")


@pytest.mark.parametrize(
    "colliding",
    [[_product(999, SELLER_CODE)], [_product(998, SELLER_CODE), _product(999, SELLER_CODE)]],
    ids=["unique-unrelated", "ambiguous-unrelated"],
)
def test_supplier_mapping_wins_over_a_global_default_code_collision(colliding: list[Product]) -> None:
    products = ProductRepository({SELLER_CODE: colliding})
    supplier = SupplierRepository(
        {(SUPPLIER_A, SELLER_CODE): [_row(SUPPLIER_A, SELLER_CODE, 39, 393)]}, {39: [_variant(393, 39)]}
    )

    result = _match(_line(seller_item_code=SELLER_CODE), products=products, supplier=supplier)

    assert result.status is ProductMatchStatus.MATCHED
    assert result.product_id == 393
    assert result.matched_by == "supplier_product_code"
    assert result.reason == "Unique product match by supplier_product_code."


def test_same_seller_code_under_two_suppliers_maps_independently() -> None:
    supplier = SupplierRepository(
        {
            (SUPPLIER_A, "TFZP"): [_row(SUPPLIER_A, "TFZP", 50, 501)],
            (SUPPLIER_B, "TFZP"): [_row(SUPPLIER_B, "TFZP", 51, 511, row_id=2)],
        },
        {50: [_variant(501, 50)], 51: [_variant(511, 51)]},
    )
    products = ProductRepository({"TFZP": [_product(999, "TFZP")]})
    line = _line(seller_item_code="TFZP")

    a = _match(line, products=products, supplier=supplier, partner_id=SUPPLIER_A)
    b = _match(line, products=products, supplier=supplier, partner_id=SUPPLIER_B)
    unmapped = _match(line, products=products, supplier=supplier, partner_id=452)

    assert (a.status, a.product_id) == (ProductMatchStatus.MATCHED, 501)
    assert (b.status, b.product_id) == (ProductMatchStatus.MATCHED, 511)
    assert (unmapped.status, unmapped.product_id) == (ProductMatchStatus.NOT_FOUND, None)


def test_mapping_works_for_a_product_without_any_default_code() -> None:
    """A #208 "Ürün Eşleştir" supplierinfo row never relies on a global default_code."""

    supplier = SupplierRepository({(SUPPLIER_A, "X-1"): [_row(SUPPLIER_A, "X-1", 80, None)]}, {80: [_variant(801, 80)]})
    products = ProductRepository()

    result = _match(_line(seller_item_code="X-1"), products=products, supplier=supplier)

    assert (result.status, result.product_id, result.matched_by) == (
        ProductMatchStatus.MATCHED,
        801,
        "supplier_product_code",
    )


# --- buyer code / barcode / SKU semantics -------------------------------------------


def test_buyer_code_default_code_is_still_deterministic() -> None:
    products = ProductRepository({"ICT-001": [_product(10, "ICT-001")]})

    result = _match(_line(buyer_item_code="ICT-001"), products=products)

    assert (result.status, result.product_id, result.matched_by) == (ProductMatchStatus.MATCHED, 10, "default_code")


def test_buyer_code_agreeing_with_the_supplier_mapping_corroborates_it() -> None:
    products = ProductRepository({"ICT-393": [_product(393, "ICT-393")]})
    supplier = SupplierRepository(
        {(SUPPLIER_A, SELLER_CODE): [_row(SUPPLIER_A, SELLER_CODE, 39, 393)]}, {39: [_variant(393, 39)]}
    )

    result = _match(
        _line(buyer_item_code="ICT-393", seller_item_code=SELLER_CODE), products=products, supplier=supplier
    )

    assert result.status is ProductMatchStatus.MATCHED
    assert result.matched_by == "supplier_product_code"
    assert result.reason == "Unique product match by supplier_product_code; corroborated by default_code."


def test_buyer_code_disagreeing_with_the_supplier_mapping_fails_closed() -> None:
    products = ProductRepository({"ICT-10": [_product(10, "ICT-10")]})
    supplier = SupplierRepository(
        {(SUPPLIER_A, SELLER_CODE): [_row(SUPPLIER_A, SELLER_CODE, 39, 393)]}, {39: [_variant(393, 39)]}
    )

    result = _match(_line(buyer_item_code="ICT-10", seller_item_code=SELLER_CODE), products=products, supplier=supplier)

    assert result.status is ProductMatchStatus.MULTIPLE_MATCHES
    assert result.product_id is None
    assert "supplier_product_code -> product 393" in result.reason
    assert "default_code -> product 10" in result.reason


# --- genuine ambiguity still fails closed -------------------------------------------


def test_duplicate_supplier_mappings_fail_closed() -> None:
    supplier = SupplierRepository(
        {
            (SUPPLIER_A, SELLER_CODE): [
                _row(SUPPLIER_A, SELLER_CODE, 39, 393),
                _row(SUPPLIER_A, SELLER_CODE, 40, 400, 2),
            ]
        },
        {39: [_variant(393, 39)], 40: [_variant(400, 40)]},
    )

    result = _match(_line(seller_item_code=SELLER_CODE), supplier=supplier)

    assert result.status is ProductMatchStatus.MULTIPLE_MATCHES
    assert result.product_id is None


def test_template_level_mapping_with_several_variants_fails_closed() -> None:
    supplier = SupplierRepository(
        {(SUPPLIER_A, SELLER_CODE): [_row(SUPPLIER_A, SELLER_CODE, 39, None)]},
        {39: [_variant(393, 39), _variant(394, 39)]},
    )

    result = _match(_line(seller_item_code=SELLER_CODE), supplier=supplier)

    assert result.status is ProductMatchStatus.MULTIPLE_MATCHES


def test_ambiguous_buyer_code_is_not_rescued_by_a_supplier_mapping() -> None:
    products = ProductRepository({"ICT-DUP": [_product(1, "ICT-DUP"), _product(2, "ICT-DUP")]})
    supplier = SupplierRepository(
        {(SUPPLIER_A, SELLER_CODE): [_row(SUPPLIER_A, SELLER_CODE, 39, 393)]}, {39: [_variant(393, 39)]}
    )

    result = _match(
        _line(buyer_item_code="ICT-DUP", seller_item_code=SELLER_CODE), products=products, supplier=supplier
    )

    assert result.status is ProductMatchStatus.MULTIPLE_MATCHES


# --- LogoSoft representative shapes -------------------------------------------------


def _logosoft_lines() -> tuple[InvoiceLine, ...]:
    # Shape of the production lines: seller code only, no buyer code/barcode/SKU.
    return (
        _line("1", seller_item_code=SELLER_CODE, description="Microsoft 365 Business Basic"),
        _line("2", seller_item_code=SELLER_CODE_2, description="Microsoft 365 Business Standard"),
    )


def _logosoft_products() -> ProductRepository:
    return ProductRepository({SELLER_CODE: [_product(393, SELLER_CODE)], SELLER_CODE_2: [_product(394, SELLER_CODE_2)]})


def test_logosoft_shapes_match_once_explicit_supplier_mappings_exist() -> None:
    # Mirrors the read-only production snapshot (2026-10-07): supplierinfo 6/7 for
    # partner 450, company 1, variant-specific on single-variant templates 162/163.
    supplier = SupplierRepository(
        {
            (SUPPLIER_A, SELLER_CODE): [_row(SUPPLIER_A, SELLER_CODE, 162, 393, row_id=6)],
            (SUPPLIER_A, SELLER_CODE_2): [_row(SUPPLIER_A, SELLER_CODE_2, 163, 394, row_id=7)],
        },
        {162: [_variant(393, 162)], 163: [_variant(394, 163)]},
    )

    results = _match_all(_invoice(*_logosoft_lines()), products=_logosoft_products(), supplier=supplier)

    assert [(r.status, r.product_id, r.matched_by) for r in results] == [
        (ProductMatchStatus.MATCHED, 393, "supplier_product_code"),
        (ProductMatchStatus.MATCHED, 394, "supplier_product_code"),
    ]
    assert all(r.reason.endswith("corroborated by seller_item_code (advisory).") for r in results)


def test_logosoft_shapes_without_supplier_mappings_are_not_found() -> None:
    """Why the transition must create the two supplierinfo rows before deploying."""

    results = _match_all(_invoice(*_logosoft_lines()), products=_logosoft_products())

    assert [(r.status, r.product_id) for r in results] == [
        (ProductMatchStatus.NOT_FOUND, None),
        (ProductMatchStatus.NOT_FOUND, None),
    ]
