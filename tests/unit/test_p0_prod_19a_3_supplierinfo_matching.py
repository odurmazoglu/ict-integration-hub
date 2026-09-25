"""P0-PROD-19A-3: supplier-scoped seller SKU matching through ``product.supplierinfo``.

- ``(uniquely MATCHED supplier partner, seller_item_code) -> exactly one supplierinfo row
  -> exactly one active variant`` is one more deterministic identity.
- It joins the 19A-2 convergence/conflict combination: agreement matches, disagreement or
  ambiguity fails closed as ``MULTIPLE_MATCHES``.
- The legacy ``seller_item_code -> default_code`` probe is unchanged.
- The Odoo reader is structurally read-only, exact, company-scoped, bounded and strict.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.application.commands import ImportInvoiceCommand
from app.application.rules.deterministic import DeterministicRuleEngine, _product_review_reasons
from app.application.workflow import ManualReviewReasonCode
from app.domain.invoice import (
    DESCRIPTION_SOURCE_DESCRIPTION,
    Header,
    InternalInvoice,
    InvoiceLine,
    MonetaryTotals,
    Party,
)
from app.erp.exceptions import ErpRepositoryResponseError
from app.erp.models import Partner, Product, ProductVariant, SupplierProductCode
from app.erp.odoo.supplier_product_repository import OdooSupplierProductRepository
from app.matching import (
    PartnerMatchingEngine,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchingEngine,
    ProductMatchingError,
    ProductMatchStatus,
)
from app.persistence.execution_source_invoice_reader import _product_line_match_from_data, _product_line_match_to_data
from app.tax_mapping import InvoiceTaxMappingResult

VITEL_PARTNER_ID = 434
OTHER_PARTNER_ID = 999
VITEL_VKN = "9250020961"
VITEL_SELLER_CODE = "1531012114"
ME_SKU = "85710.1S1"
COMPANY_ID = 1
ME_TEMPLATE_ID = 41


# --- fakes --------------------------------------------------------------------------


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


class FakeSupplierProductRepository:
    def __init__(
        self,
        rows: dict[tuple[int, str], list[SupplierProductCode]] | None = None,
        variants: dict[int, list[ProductVariant]] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self.rows = rows or {}
        self.variants = variants or {}
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def find_supplier_product_codes(
        self, *, partner_id: int, product_code: str, company_id: int, limit: int
    ) -> Sequence[SupplierProductCode]:
        self.calls.append(
            (
                "supplierinfo",
                {"partner_id": partner_id, "product_code": product_code, "company_id": company_id, "limit": limit},
            )
        )
        if self.fail:
            raise RuntimeError("odoo down")
        return tuple(self.rows.get((partner_id, product_code), ())[:limit])

    def find_template_variants(
        self, *, product_tmpl_id: int, company_id: int, variant_id: int | None, limit: int
    ) -> Sequence[ProductVariant]:
        self.calls.append(
            (
                "variants",
                {
                    "product_tmpl_id": product_tmpl_id,
                    "company_id": company_id,
                    "variant_id": variant_id,
                    "limit": limit,
                },
            )
        )
        candidates = self.variants.get(product_tmpl_id, [])
        if variant_id is not None:
            candidates = [variant for variant in candidates if variant.id == variant_id]
        return tuple(candidates[:limit])


class FakeProvider:
    def __init__(self, product_repository: RecordingProductRepository) -> None:
        self.product_repository = product_repository


def _product(product_id: int, default_code: str | None = None) -> Product:
    return Product(id=product_id, name=f"P{product_id}", default_code=default_code, barcode=None, active=True)


def _row(
    product_id: int | None, *, partner_id: int = VITEL_PARTNER_ID, template_id: int = ME_TEMPLATE_ID, row_id: int = 7
):
    return SupplierProductCode(
        id=row_id,
        partner_id=partner_id,
        product_code=VITEL_SELLER_CODE,
        product_tmpl_id=template_id,
        product_id=product_id,
        company_id=COMPANY_ID,
    )


def _variant(variant_id: int, *, template_id: int = ME_TEMPLATE_ID, active: bool = True) -> ProductVariant:
    return ProductVariant(id=variant_id, product_tmpl_id=template_id, active=active, company_id=None)


def _partner_match(
    status: PartnerMatchStatus = PartnerMatchStatus.MATCHED, partner_id: int | None = VITEL_PARTNER_ID
) -> PartnerMatchResult:
    matched = status is PartnerMatchStatus.MATCHED
    return PartnerMatchResult(
        status=status,
        partner_id=partner_id if matched else None,
        matched_by="tax_number" if matched else None,
        reason="test",
        candidate_count=1 if matched else 0,
        confidence=Decimal("1.00") if matched else None,
    )


def _invoice(lines: list[InvoiceLine], *, vkn: str = VITEL_VKN) -> InternalInvoice:
    return InternalInvoice(
        header=Header(invoice_number="INV-19A3", invoice_uuid="uuid-19a3"),
        supplier=Party(tax_number=vkn),
        customer=Party(),
        totals=MonetaryTotals(),
        lines=tuple(lines),
    )


def _line(**kwargs: Any) -> InvoiceLine:
    kwargs.setdefault("seller_item_code", VITEL_SELLER_CODE)
    return InvoiceLine(line_number="1", **kwargs)


def _vitel_sku_line(**kwargs: Any) -> InvoiceLine:
    return _line(description=ME_SKU, description_source=DESCRIPTION_SOURCE_DESCRIPTION, **kwargs)


def _match(
    invoice: InternalInvoice,
    *,
    products: RecordingProductRepository | None = None,
    supplier: FakeSupplierProductRepository | None = None,
    partner_match: PartnerMatchResult | None = None,
    company_id: int | None = COMPANY_ID,
    wired: bool = True,
) -> Any:
    engine = ProductMatchingEngine(
        FakeProvider(products or RecordingProductRepository()),
        supplier_product_repository=(supplier or FakeSupplierProductRepository()) if wired else None,
    )
    result = engine.match_invoice(
        invoice,
        company_id=company_id,
        partner_match=partner_match if partner_match is not None else _partner_match(),
    )
    return result.line_results[0].result


def _vitel_supplierinfo(product_id: int | None = 392, **variant_kwargs: Any) -> FakeSupplierProductRepository:
    variants = variant_kwargs.pop("variants", [_variant(392)])
    return FakeSupplierProductRepository(
        {(VITEL_PARTNER_ID, VITEL_SELLER_CODE): [_row(product_id)]}, {ME_TEMPLATE_ID: variants}
    )


# --- 1, 5: supplierinfo resolves one exact variant ----------------------------------


def test_resolved_supplier_and_seller_code_match_the_supplierinfo_variant() -> None:
    supplier = _vitel_supplierinfo()
    products = RecordingProductRepository()

    result = _match(_invoice([_line()]), products=products, supplier=supplier)

    assert result.status is ProductMatchStatus.MATCHED
    assert result.product_id == 392
    assert result.matched_by == "supplier_product_code"
    assert supplier.calls == [
        (
            "supplierinfo",
            {"partner_id": VITEL_PARTNER_ID, "product_code": VITEL_SELLER_CODE, "company_id": COMPANY_ID, "limit": 2},
        ),
        ("variants", {"product_tmpl_id": ME_TEMPLATE_ID, "company_id": COMPANY_ID, "variant_id": 392, "limit": 2}),
    ]
    # The legacy seller-code probe still runs and never becomes a default_code write.
    assert products.calls == [("default_code", VITEL_SELLER_CODE)]


def test_seller_code_whitespace_is_stripped_like_every_other_identifier() -> None:
    supplier = _vitel_supplierinfo()

    result = _match(_invoice([_line(seller_item_code=f"  {VITEL_SELLER_CODE} ")]), supplier=supplier)

    assert result.status is ProductMatchStatus.MATCHED
    assert supplier.calls[0][1]["product_code"] == VITEL_SELLER_CODE


# --- 2, 3, 4: supplier scoping ------------------------------------------------------


def test_same_seller_code_under_another_supplier_is_ignored() -> None:
    supplier = FakeSupplierProductRepository(
        {(OTHER_PARTNER_ID, VITEL_SELLER_CODE): [_row(500, partner_id=OTHER_PARTNER_ID)]},
        {ME_TEMPLATE_ID: [_variant(500)]},
    )

    result = _match(_invoice([_line()]), supplier=supplier)

    assert result.status is ProductMatchStatus.NOT_FOUND
    assert [call[1]["partner_id"] for call in supplier.calls if call[0] == "supplierinfo"] == [VITEL_PARTNER_ID]


@pytest.mark.parametrize(
    "partner_match",
    [
        _partner_match(PartnerMatchStatus.NOT_FOUND),
        _partner_match(PartnerMatchStatus.MULTIPLE_MATCHES),
        _partner_match(PartnerMatchStatus.INVALID_INPUT),
    ],
    ids=["not-found", "ambiguous", "invalid"],
)
def test_unresolved_or_ambiguous_supplier_never_queries_supplierinfo(partner_match: PartnerMatchResult) -> None:
    supplier = _vitel_supplierinfo()

    result = _match(_invoice([_line()]), supplier=supplier, partner_match=partner_match)

    assert supplier.calls == []
    assert result.status is ProductMatchStatus.NOT_FOUND


@pytest.mark.parametrize("partner_id", [None, 0, -1])
def test_matched_status_without_a_positive_partner_id_never_queries_supplierinfo(partner_id: int | None) -> None:
    supplier = _vitel_supplierinfo()
    partner_match = PartnerMatchResult(
        status=PartnerMatchStatus.MATCHED,
        partner_id=partner_id,
        matched_by="tax_number",
        reason="x",
        candidate_count=1,
        confidence=None,
    )

    _match(_invoice([_line()]), supplier=supplier, partner_match=partner_match)

    assert supplier.calls == []


def test_no_partner_match_argument_never_queries_supplierinfo() -> None:
    supplier = _vitel_supplierinfo()
    engine = ProductMatchingEngine(FakeProvider(RecordingProductRepository()), supplier_product_repository=supplier)

    result = engine.match_invoice(_invoice([_line()]), company_id=COMPANY_ID).line_results[0].result

    assert supplier.calls == []
    assert result.status is ProductMatchStatus.NOT_FOUND


def test_unknown_company_never_queries_supplierinfo() -> None:
    supplier = _vitel_supplierinfo()

    _match(_invoice([_line()]), supplier=supplier, company_id=None)

    assert supplier.calls == []


def test_missing_seller_code_never_queries_supplierinfo() -> None:
    supplier = _vitel_supplierinfo()

    result = _match(_invoice([_line(seller_item_code=None, buyer_item_code="B")]), supplier=supplier)

    assert supplier.calls == []
    assert result.status is ProductMatchStatus.NOT_FOUND


# --- 6, 7, 8: convergence and conflicts ---------------------------------------------


def test_supplierinfo_and_manageengine_sku_agreeing_match() -> None:
    products = RecordingProductRepository({ME_SKU: [_product(392, ME_SKU)]})

    result = _match(_invoice([_vitel_sku_line()]), products=products, supplier=_vitel_supplierinfo())

    assert result.status is ProductMatchStatus.MATCHED
    assert result.product_id == 392
    assert result.matched_by == "supplier_profile_sku"
    assert "corroborated by supplier_product_code" in result.reason


def test_supplierinfo_and_manageengine_sku_disagreeing_fail_closed() -> None:
    products = RecordingProductRepository({ME_SKU: [_product(392, ME_SKU)]})
    supplier = _vitel_supplierinfo(500, variants=[_variant(500)])

    result = _match(_invoice([_vitel_sku_line()]), products=products, supplier=supplier)

    assert result.status is ProductMatchStatus.MULTIPLE_MATCHES
    assert result.product_id is None
    assert "supplier_profile_sku -> product 392" in result.reason
    assert "supplier_product_code -> product 500" in result.reason


def test_supplierinfo_and_manufacturer_item_code_disagreeing_fail_closed() -> None:
    products = RecordingProductRepository({"85710.1S3": [_product(393)]})

    result = _match(
        _invoice([_line(manufacturer_item_code="85710.1S3")], vkn="1111111111"),
        products=products,
        supplier=_vitel_supplierinfo(),
    )

    assert result.status is ProductMatchStatus.MULTIPLE_MATCHES
    assert result.reason.startswith("Conflicting product identities")


@pytest.mark.parametrize(
    ("line_kwargs", "products"),
    [
        ({"buyer_item_code": "BUY-1"}, RecordingProductRepository({"BUY-1": [_product(10)]})),
        ({"barcode": "869"}, RecordingProductRepository(barcode_records={"869": [_product(10)]})),
        ({}, RecordingProductRepository({VITEL_SELLER_CODE: [_product(10)]})),
    ],
    ids=["buyer", "barcode", "legacy-seller-default-code"],
)
def test_supplierinfo_and_legacy_identity_disagreeing_fail_closed(
    line_kwargs: dict[str, str], products: RecordingProductRepository
) -> None:
    result = _match(_invoice([_line(**line_kwargs)]), products=products, supplier=_vitel_supplierinfo())

    assert result.status is ProductMatchStatus.MULTIPLE_MATCHES
    assert result.product_id is None
    assert result.reason.startswith("Conflicting product identities")


def test_conflict_surfaces_as_product_ambiguous_review_reason() -> None:
    invoice = _invoice([_vitel_sku_line()])
    engine = ProductMatchingEngine(
        FakeProvider(RecordingProductRepository({ME_SKU: [_product(392)]})),
        supplier_product_repository=_vitel_supplierinfo(500, variants=[_variant(500)]),
    )
    result = engine.match_invoice(invoice, company_id=COMPANY_ID, partner_match=_partner_match())

    assert [reason.code for reason in _product_review_reasons(invoice, result)] == [
        ManualReviewReasonCode.PRODUCT_AMBIGUOUS
    ]


# --- 9, 10, 11: ambiguity and variant precision -------------------------------------


def test_duplicate_supplierinfo_rows_fail_closed_even_for_the_same_variant() -> None:
    supplier = FakeSupplierProductRepository(
        {(VITEL_PARTNER_ID, VITEL_SELLER_CODE): [_row(392, row_id=7), _row(392, row_id=8)]},
        {ME_TEMPLATE_ID: [_variant(392)]},
    )

    result = _match(_invoice([_line()]), supplier=supplier)

    assert result.status is ProductMatchStatus.MULTIPLE_MATCHES
    assert result.candidate_count == 2
    assert result.reason == "Multiple active product candidates found by supplier_product_code."
    assert [call[0] for call in supplier.calls] == ["supplierinfo"]


def test_template_level_row_with_several_active_variants_fails_closed() -> None:
    supplier = _vitel_supplierinfo(None, variants=[_variant(392), _variant(393), _variant(394)])

    result = _match(_invoice([_line()]), supplier=supplier)

    assert result.status is ProductMatchStatus.MULTIPLE_MATCHES
    assert result.product_id is None
    assert supplier.calls[1][1]["variant_id"] is None


def test_template_level_row_with_exactly_one_active_variant_identifies_it() -> None:
    supplier = _vitel_supplierinfo(None, variants=[_variant(600, template_id=ME_TEMPLATE_ID)])

    result = _match(_invoice([_line()]), supplier=supplier)

    assert result.status is ProductMatchStatus.MATCHED
    assert result.product_id == 600


def test_archived_variant_named_by_supplierinfo_is_not_matched_and_not_replaced() -> None:
    supplier = _vitel_supplierinfo(392, variants=[_variant(392, active=False), _variant(393)])

    result = _match(_invoice([_line()]), supplier=supplier)

    assert result.status is ProductMatchStatus.NOT_FOUND
    assert result.product_id is None


def test_variant_outside_the_company_scope_is_not_matched() -> None:
    # The Odoo domain scopes to [company_id, False]; an out-of-scope variant is simply absent.
    supplier = _vitel_supplierinfo(392, variants=[])

    result = _match(_invoice([_line()]), supplier=supplier)

    assert result.status is ProductMatchStatus.NOT_FOUND


def test_supplier_repository_failure_is_not_swallowed() -> None:
    with pytest.raises(ProductMatchingError):
        _match(_invoice([_line()]), supplier=FakeSupplierProductRepository(fail=True))


# --- 12, 13: legacy and non-VİTEL compatibility -------------------------------------


def test_legacy_seller_code_default_code_match_is_unchanged_without_supplierinfo() -> None:
    products = RecordingProductRepository({"SUP-1": [_product(30, "SUP-1")]})
    supplier = FakeSupplierProductRepository()

    result = _match(_invoice([_line(seller_item_code="SUP-1")], vkn="1111111111"), products=products, supplier=supplier)

    assert result.status is ProductMatchStatus.MATCHED
    assert result.product_id == 30
    assert result.matched_by == "seller_item_code"
    assert result.reason == "Unique product match by seller_item_code."


def test_legacy_seller_code_and_supplierinfo_converge() -> None:
    products = RecordingProductRepository({VITEL_SELLER_CODE: [_product(392)]})

    result = _match(_invoice([_line()]), products=products, supplier=_vitel_supplierinfo())

    assert result.status is ProductMatchStatus.MATCHED
    assert result.matched_by == "seller_item_code"
    assert "corroborated by supplier_product_code" in result.reason


@pytest.mark.parametrize("wired", [True, False])
def test_supplier_without_supplierinfo_rows_keeps_exact_legacy_calls_and_result(wired: bool) -> None:
    products = RecordingProductRepository({"BUY-1": [_product(10)]})
    line = _line(buyer_item_code="BUY-1", seller_item_code="S-9")

    result = _match(_invoice([line], vkn="1111111111"), products=products, wired=wired)

    assert products.calls == [("default_code", "BUY-1")]
    assert result.status is ProductMatchStatus.MATCHED
    assert result.matched_by == "default_code"
    assert result.reason == "Unique product match by default_code."


# --- rule engine wiring -------------------------------------------------------------


class _FakePartnerRepository:
    def find_by_tax_number(self, tax_number: str, *, company_id: int | None = None) -> Sequence[Partner]:
        del company_id
        if tax_number == VITEL_VKN:
            return (Partner(id=VITEL_PARTNER_ID, name="VITEL", tax_number=VITEL_VKN, active=True),)
        return ()


class _FakeRuleProvider(FakeProvider):
    def __init__(self, product_repository: RecordingProductRepository) -> None:
        super().__init__(product_repository)
        self.partner_repository = _FakePartnerRepository()


class _NoTaxMapper:
    def map_invoice(self, invoice: InternalInvoice, *, company_id: int | None = None) -> InvoiceTaxMappingResult:
        del invoice, company_id
        return InvoiceTaxMappingResult()


@pytest.mark.parametrize(
    ("vkn", "expected"), [(VITEL_VKN, ProductMatchStatus.MATCHED), ("1", ProductMatchStatus.NOT_FOUND)]
)
def test_rule_engine_passes_the_deterministic_partner_match_to_product_matching(
    vkn: str, expected: ProductMatchStatus
) -> None:
    provider = _FakeRuleProvider(RecordingProductRepository())
    supplier = _vitel_supplierinfo()
    engine = DeterministicRuleEngine(
        partner_matcher=PartnerMatchingEngine(provider),
        product_matcher=ProductMatchingEngine(provider, supplier_product_repository=supplier),
        tax_mapper=_NoTaxMapper(),
    )

    result = engine.evaluate(
        ImportInvoiceCommand(invoice=_invoice([_line()], vkn=vkn), idempotency_key="k", company_id=1)
    )

    assert result.product_match.line_results[0].result.status is expected
    assert bool(supplier.calls) is (vkn == VITEL_VKN)


def test_production_decision_engine_wires_the_read_only_supplier_product_repository() -> None:
    from app.composition.imports import _build_deterministic_decision_engine, build_odoo_read_repository_provider
    from app.erp.odoo.adapter import OdooReadOnlyAdapter

    adapter = OdooReadOnlyAdapter(client=object())  # type: ignore[arg-type]
    engine = _build_deterministic_decision_engine(
        session=object(),  # type: ignore[arg-type]
        provider=build_odoo_read_repository_provider(read_adapter=adapter),
        read_adapter=adapter,
    )

    product_matcher = engine._rule_engine._product_matcher  # type: ignore[attr-defined]
    assert isinstance(product_matcher._supplier_product_repository, OdooSupplierProductRepository)


# --- 14: evidence compatibility -----------------------------------------------------


LEGACY_PRODUCT_MATCH_KEYS = {
    "status",
    "line_number",
    "product_id",
    "default_code",
    "barcode",
    "seller_item_code",
    "matched_by",
    "reason",
    "candidate_count",
    "confidence",
}


@pytest.mark.parametrize("conflict", [False, True])
def test_supplierinfo_results_persist_in_the_existing_product_match_shape(conflict: bool) -> None:
    products = RecordingProductRepository({ME_SKU: [_product(392)]})
    supplier = _vitel_supplierinfo(500 if conflict else 392, variants=[_variant(500 if conflict else 392)])
    result = _match(_invoice([_vitel_sku_line()]), products=products, supplier=supplier)

    data = _product_line_match_to_data(result)

    assert set(data) == LEGACY_PRODUCT_MATCH_KEYS
    assert data["status"] in {"MATCHED", "NOT_FOUND", "MULTIPLE_MATCHES", "INVALID_INPUT"}
    assert _product_line_match_from_data(data) == result


# --- 15: Odoo reader is exact, bounded, strict and read-only ------------------------


class _RecordingAdapter:
    def __init__(self, records: list[Any]) -> None:
        self.records = records
        self.calls: list[dict[str, Any]] = []

    def search_read(self, *, model: str, domain: list[Any], fields: list[str], limit: int | None = None) -> tuple:
        self.calls.append({"model": model, "domain": domain, "fields": fields, "limit": limit})
        return tuple(self.records)


def _repo(records: list[Any]) -> tuple[OdooSupplierProductRepository, _RecordingAdapter]:
    adapter = _RecordingAdapter(records)
    return OdooSupplierProductRepository(adapter=adapter), adapter  # type: ignore[arg-type]


def _raw_row(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": 7,
        "partner_id": [VITEL_PARTNER_ID, "VITEL"],
        "product_code": VITEL_SELLER_CODE,
        "product_tmpl_id": [ME_TEMPLATE_ID, "ManageEngine Endpoint Central"],
        "product_id": [392, "ManageEngine Endpoint Central (85710.1S1)"],
        "company_id": False,
    }
    record.update(overrides)
    return record


def _find_rows(repository: OdooSupplierProductRepository) -> Sequence[SupplierProductCode]:
    return repository.find_supplier_product_codes(
        partner_id=VITEL_PARTNER_ID, product_code=VITEL_SELLER_CODE, company_id=COMPANY_ID, limit=2
    )


def test_supplierinfo_domain_is_exact_company_scoped_and_bounded() -> None:
    repository, adapter = _repo([_raw_row(), _raw_row(id=8, product_id=False, company_id=[COMPANY_ID, "ICT"])])

    rows = _find_rows(repository)

    assert adapter.calls == [
        {
            "model": "product.supplierinfo",
            "domain": [
                ["partner_id", "=", VITEL_PARTNER_ID],
                ["product_code", "=", VITEL_SELLER_CODE],
                ["company_id", "in", [COMPANY_ID, False]],
            ],
            "fields": ["id", "partner_id", "product_code", "product_tmpl_id", "product_id", "company_id"],
            "limit": 2,
        }
    ]
    assert rows == (
        SupplierProductCode(7, VITEL_PARTNER_ID, VITEL_SELLER_CODE, ME_TEMPLATE_ID, 392, None),
        SupplierProductCode(8, VITEL_PARTNER_ID, VITEL_SELLER_CODE, ME_TEMPLATE_ID, None, COMPANY_ID),
    )


@pytest.mark.parametrize(
    "record",
    [
        _raw_row(partner_id=[OTHER_PARTNER_ID, "Other"]),
        _raw_row(product_code="1531012114 "),
        _raw_row(product_code="other"),
        _raw_row(company_id=[2, "Other company"]),
        _raw_row(product_tmpl_id=False),
        _raw_row(product_id="392"),
        _raw_row(id=True),
        _raw_row(id=0),
        "not-a-record",
    ],
    ids=[
        "other-partner",
        "code-whitespace",
        "other-code",
        "other-company",
        "no-template",
        "bad-variant",
        "bool-id",
        "zero-id",
        "non-dict",
    ],
)
def test_supplierinfo_record_outside_the_requested_identity_fails_closed(record: Any) -> None:
    repository, _ = _repo([record])

    with pytest.raises(ErpRepositoryResponseError):
        _find_rows(repository)


def test_supplierinfo_response_larger_than_the_limit_fails_closed() -> None:
    repository, _ = _repo([_raw_row(id=7), _raw_row(id=8), _raw_row(id=9)])

    with pytest.raises(ErpRepositoryResponseError):
        _find_rows(repository)


def test_variant_domain_is_exact_company_scoped_and_bounded() -> None:
    repository, adapter = _repo(
        [{"id": 392, "product_tmpl_id": [ME_TEMPLATE_ID, "T"], "active": True, "company_id": False}]
    )

    variants = repository.find_template_variants(
        product_tmpl_id=ME_TEMPLATE_ID, company_id=COMPANY_ID, variant_id=392, limit=2
    )

    assert adapter.calls == [
        {
            "model": "product.product",
            "domain": [
                ["product_tmpl_id", "=", ME_TEMPLATE_ID],
                ["company_id", "in", [COMPANY_ID, False]],
                ["id", "=", 392],
            ],
            "fields": ["id", "product_tmpl_id", "active", "company_id"],
            "limit": 2,
        }
    ]
    assert variants == (ProductVariant(id=392, product_tmpl_id=ME_TEMPLATE_ID, active=True, company_id=None),)


@pytest.mark.parametrize(
    "record",
    [
        {"id": 393, "product_tmpl_id": [ME_TEMPLATE_ID, "T"], "active": True, "company_id": False},
        {"id": 392, "product_tmpl_id": [99, "Other"], "active": True, "company_id": False},
        {"id": 392, "product_tmpl_id": [ME_TEMPLATE_ID, "T"], "active": True, "company_id": [2, "Other"]},
        {"id": 392, "product_tmpl_id": [ME_TEMPLATE_ID, "T"], "active": None, "company_id": False},
    ],
    ids=["other-variant", "other-template", "other-company", "missing-active"],
)
def test_variant_record_outside_the_requested_template_fails_closed(record: dict[str, Any]) -> None:
    repository, _ = _repo([record])

    with pytest.raises(ErpRepositoryResponseError):
        repository.find_template_variants(
            product_tmpl_id=ME_TEMPLATE_ID, company_id=COMPANY_ID, variant_id=392, limit=2
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"partner_id": 0, "product_code": VITEL_SELLER_CODE, "company_id": 1, "limit": 2},
        {"partner_id": 434, "product_code": "", "company_id": 1, "limit": 2},
        {"partner_id": 434, "product_code": " 1531012114", "company_id": 1, "limit": 2},
        {"partner_id": 434, "product_code": VITEL_SELLER_CODE, "company_id": 0, "limit": 2},
        {"partner_id": 434, "product_code": VITEL_SELLER_CODE, "company_id": 1, "limit": 0},
    ],
)
def test_invalid_lookup_arguments_are_rejected_before_any_odoo_call(kwargs: dict[str, Any]) -> None:
    repository, adapter = _repo([])

    with pytest.raises(ValueError):
        repository.find_supplier_product_codes(**kwargs)
    assert adapter.calls == []


def test_supplier_product_reader_is_structurally_read_only() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "app" / "erp" / "odoo" / "supplier_product_repository.py"
    ).read_text()

    for token in ("create", "write", "unlink", "action_post", "OdooJson2Client", "app.erp.write", "call_kw"):
        assert token not in source
    assert "self._adapter.search_read(" in source
    assert "OdooReadOnlyAdapter" in source
