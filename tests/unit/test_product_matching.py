from __future__ import annotations

from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party
from app.erp.models import Product
from app.matching import ProductMatchingEngine, ProductMatchStatus


class FakeProductRepository:
    def __init__(
        self,
        *,
        default_code_records: dict[str, Sequence[Product]] | None = None,
        barcode_records: dict[str, Sequence[Product]] | None = None,
        fail: bool = False,
    ) -> None:
        self.default_code_records = default_code_records or {}
        self.barcode_records = barcode_records or {}
        self.fail = fail
        self.calls: list[tuple[str, str, int | None]] = []

    def find_by_default_code(self, default_code: str, *, company_id: int | None = None) -> Sequence[Product]:
        self.calls.append(("default_code", default_code, company_id))
        if self.fail:
            raise RuntimeError("repository failed")
        return tuple(self.default_code_records.get(default_code, ()))

    def find_by_barcode(self, barcode: str, *, company_id: int | None = None) -> Sequence[Product]:
        self.calls.append(("barcode", barcode, company_id))
        if self.fail:
            raise RuntimeError("repository failed")
        return tuple(self.barcode_records.get(barcode, ()))

    def find_by_ids(self, ids: Sequence[int]) -> Sequence[Product]:
        del ids
        return ()


class FakeProvider:
    def __init__(self, product_repository: FakeProductRepository) -> None:
        self.product_repository = product_repository


def test_exact_default_code_match() -> None:
    repository = FakeProductRepository(default_code_records={"SKU-1": [_product(10, default_code="SKU-1")]})

    result = _match(_invoice([_line("1", buyer_item_code="SKU-1")]), repository)

    line_result = result.line_results[0].result
    assert line_result.status is ProductMatchStatus.MATCHED
    assert line_result.product_id == 10
    assert line_result.matched_by == "default_code"
    assert line_result.confidence == Decimal("1.00")
    assert line_result.candidate_count == 1
    assert repository.calls == [("default_code", "SKU-1", None)]


def test_exact_barcode_match_after_default_code_not_found() -> None:
    repository = FakeProductRepository(barcode_records={"869": [_product(20, barcode="869")]})

    result = _match(_invoice([_line("1", buyer_item_code="SKU-404", barcode="869")]), repository, company_id=7)

    line_result = result.line_results[0].result
    assert line_result.status is ProductMatchStatus.MATCHED
    assert line_result.product_id == 20
    assert line_result.matched_by == "barcode"
    assert repository.calls == [("default_code", "SKU-404", 7), ("barcode", "869", 7)]


def test_exact_seller_item_code_match_after_higher_priority_not_found() -> None:
    repository = FakeProductRepository(default_code_records={"SUP-1": [_product(30, default_code="SUP-1")]})

    result = _match(
        _invoice([_line("1", buyer_item_code="SKU-404", barcode="404", seller_item_code="SUP-1")]), repository
    )

    line_result = result.line_results[0].result
    assert line_result.status is ProductMatchStatus.MATCHED
    assert line_result.product_id == 30
    assert line_result.matched_by == "seller_item_code"
    assert repository.calls == [
        ("default_code", "SKU-404", None),
        ("barcode", "404", None),
        ("default_code", "SUP-1", None),
    ]


def test_priority_order_stops_after_unique_default_code_match() -> None:
    repository = FakeProductRepository(
        default_code_records={
            "SKU-1": [_product(10, default_code="SKU-1")],
            "SUP-1": [_product(30, default_code="SUP-1")],
        },
        barcode_records={"869": [_product(20, barcode="869")]},
    )

    result = _match(
        _invoice([_line("1", buyer_item_code="SKU-1", barcode="869", seller_item_code="SUP-1")]), repository
    )

    assert result.line_results[0].result.product_id == 10
    assert result.line_results[0].result.matched_by == "default_code"
    assert repository.calls == [("default_code", "SKU-1", None)]


def test_multiple_matches_do_not_fall_through_to_lower_priority_identifier() -> None:
    repository = FakeProductRepository(
        default_code_records={"SKU-1": [_product(10, default_code="SKU-1"), _product(11, default_code="SKU-1")]},
        barcode_records={"869": [_product(20, barcode="869")]},
    )

    result = _match(_invoice([_line("1", buyer_item_code="SKU-1", barcode="869")]), repository)

    line_result = result.line_results[0].result
    assert line_result.status is ProductMatchStatus.MULTIPLE_MATCHES
    assert line_result.product_id is None
    assert line_result.confidence is None
    assert line_result.candidate_count == 2
    assert repository.calls == [("default_code", "SKU-1", None)]


def test_not_found_when_all_deterministic_identifiers_miss() -> None:
    repository = FakeProductRepository()

    result = _match(
        _invoice([_line("1", buyer_item_code="SKU-404", barcode="404", seller_item_code="SUP-404")]), repository
    )

    line_result = result.line_results[0].result
    assert line_result.status is ProductMatchStatus.NOT_FOUND
    assert line_result.reason == "No active deterministic product candidate found."
    assert line_result.confidence is None
    assert repository.calls == [
        ("default_code", "SKU-404", None),
        ("barcode", "404", None),
        ("default_code", "SUP-404", None),
    ]


def test_missing_identifiers_are_invalid_input() -> None:
    repository = FakeProductRepository()

    result = _match(_invoice([_line("1")]), repository)

    line_result = result.line_results[0].result
    assert line_result.status is ProductMatchStatus.INVALID_INPUT
    assert line_result.reason == "At least one deterministic product identifier is required."
    assert line_result.candidate_count == 0
    assert repository.calls == []


def test_repository_failure_is_invalid_input_without_leaking_exception_text() -> None:
    repository = FakeProductRepository(default_code_records={"SKU-1": [_product(10)]}, fail=True)

    result = _match(_invoice([_line("1", buyer_item_code="SKU-1")]), repository)

    line_result = result.line_results[0].result
    assert line_result.status is ProductMatchStatus.INVALID_INPUT
    assert line_result.reason == "Product repository lookup failed."
    assert "repository failed" not in line_result.reason


def test_multiple_invoice_lines_are_matched_independently() -> None:
    repository = FakeProductRepository(
        default_code_records={"SKU-1": [_product(10, default_code="SKU-1")]},
        barcode_records={"869": [_product(20, barcode="869")]},
    )

    result = _match(_invoice([_line("1", buyer_item_code="SKU-1"), _line("2", barcode="869")]), repository)

    assert [line.result.status for line in result.line_results] == [
        ProductMatchStatus.MATCHED,
        ProductMatchStatus.MATCHED,
    ]
    assert [line.result.product_id for line in result.line_results] == [10, 20]


def test_empty_invoice_missing_lines_and_invalid_dto_are_rejected() -> None:
    repository = FakeProductRepository()
    engine = ProductMatchingEngine(FakeProvider(repository))

    empty_result = engine.match_invoice(_invoice([]))
    invalid_result = engine.match_invoice(object())
    missing_line_number = engine.match_invoice(_invoice([_line(None, buyer_item_code="SKU-1")]))

    assert empty_result.errors == ("Invoice has no lines to match.",)
    assert invalid_result.errors == ("InternalInvoice DTO is required for product matching.",)
    assert missing_line_number.line_results[0].result.status is ProductMatchStatus.INVALID_INPUT


def test_result_dtos_are_immutable() -> None:
    repository = FakeProductRepository(default_code_records={"SKU-1": [_product(10)]})

    result = _match(_invoice([_line("1", buyer_item_code="SKU-1")]), repository)

    with pytest.raises(FrozenInstanceError):
        result.line_results[0].result.product_id = 99  # type: ignore[misc]


def test_product_matching_package_has_no_provider_write_or_fuzzy_dependency() -> None:
    package_root = Path(__file__).resolve().parents[2] / "app" / "matching"
    combined_source = "\n".join(path.read_text() for path in package_root.rglob("*.py"))

    assert "OdooReadOnlyAdapter" not in combined_source
    assert "OdooJson2Client" not in combined_source
    assert "search_read" not in combined_source
    assert "sqlalchemy" not in combined_source.lower()
    assert "app.db" not in combined_source
    assert "create" not in combined_source
    assert "write" not in combined_source
    assert "unlink" not in combined_source
    assert "action_post" not in combined_source
    assert "description" not in combined_source
    assert "name" not in combined_source
    assert "fuzzy" not in combined_source.lower()
    assert "levenshtein" not in combined_source.lower()
    assert "embedding" not in combined_source.lower()
    assert "similarity" not in combined_source.lower()


def _match(
    invoice: InternalInvoice,
    repository: FakeProductRepository,
    *,
    company_id: int | None = None,
):
    return ProductMatchingEngine(FakeProvider(repository)).match_invoice(invoice, company_id=company_id)


def _invoice(lines: list[InvoiceLine]) -> InternalInvoice:
    return InternalInvoice(
        header=Header(invoice_number="INV-1", invoice_uuid="uuid-1", currency_code="TRY"),
        supplier=Party(name="Supplier"),
        customer=Party(name="Customer"),
        totals=MonetaryTotals(payable_amount=Decimal("1.00")),
        lines=tuple(lines),
    )


def _line(
    line_number: str | None,
    *,
    buyer_item_code: str | None = None,
    barcode: str | None = None,
    seller_item_code: str | None = None,
) -> InvoiceLine:
    return InvoiceLine(
        line_number=line_number,
        description="Ignored line description",
        buyer_item_code=buyer_item_code,
        barcode=barcode,
        seller_item_code=seller_item_code,
    )


def _product(
    product_id: int,
    *,
    default_code: str | None = None,
    barcode: str | None = None,
    active: bool = True,
) -> Product:
    return Product(
        id=product_id,
        name="Ignored ERP product name",
        default_code=default_code,
        barcode=barcode,
        active=active,
    )
