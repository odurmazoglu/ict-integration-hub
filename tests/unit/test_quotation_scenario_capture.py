from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.application.quotation.capture import CaptureQuotationScenarioCommand, CaptureQuotationScenarioUseCase
from app.application.quotation.contracts import QuotationScenarioSnapshot
from app.application.quotation.source import QuotationScenarioSource, QuotationScenarioSourceLine
from app.application.workbench.exceptions import WorkbenchContractError


def _source(
    *,
    selected: bool = True,
    company_id: int = 1,
    currency: str = "try",
    lines: tuple[QuotationScenarioSourceLine, ...] | None = None,
) -> QuotationScenarioSource:
    return QuotationScenarioSource(
        scenario_id="scenario-1",
        scenario_name="Scenario A",
        selected=selected,
        company_id=company_id,
        customer_id=20,
        opportunity_id=30,
        currency=currency,
        lines=lines
        or (
            QuotationScenarioSourceLine(
                line_id="line-2",
                product_variant_id=102,
                quantity=Decimal("2"),
                sales_unit_price=Decimal("0"),
                cost_unit_price=Decimal("4.50"),
                description="Second",
                uom_id=1,
                sequence=Decimal("2"),
            ),
            QuotationScenarioSourceLine(
                line_id="line-1",
                product_variant_id=101,
                quantity=Decimal("1.5"),
                sales_unit_price=Decimal("10.25"),
                cost_unit_price=None,
                description="First",
                uom_id=1,
                sequence=Decimal("1"),
            ),
        ),
    )


class FakeReader:
    def __init__(self, source: QuotationScenarioSource) -> None:
        self.source = source
        self.calls: list[tuple[str, int]] = []

    def get_scenario(self, *, scenario_id: str, company_id: int) -> QuotationScenarioSource:
        self.calls.append((scenario_id, company_id))
        return self.source


@dataclass(frozen=True, slots=True)
class Product:
    id: int
    active: bool = True
    company_id: int | None = 1


class FakeProductVariantReader:
    def __init__(self, products: Sequence[Product] = (Product(101), Product(102))) -> None:
        self.products = tuple(products)
        self.calls: list[tuple[int, ...]] = []

    def find_by_ids(self, ids: Sequence[int]) -> Sequence[Product]:
        self.calls.append(tuple(ids))
        return self.products


def _command(**overrides: object) -> CaptureQuotationScenarioCommand:
    values: dict[str, object] = {
        "review_id": "review-1",
        "decision_id": "decision-1",
        "decision_version": 2,
        "company_id": 1,
        "scenario_id": "scenario-1",
    }
    values.update(overrides)
    return CaptureQuotationScenarioCommand(**values)


def _execute(
    source: QuotationScenarioSource,
    *,
    products: Sequence[Product] = (Product(101), Product(102)),
) -> tuple[QuotationScenarioSnapshot, FakeReader, FakeProductVariantReader]:
    reader = FakeReader(_source())
    reader.source = source
    product_reader = FakeProductVariantReader(products)
    snapshot = CaptureQuotationScenarioUseCase(
        source_reader=reader,
        product_variant_reader=product_reader,
    ).execute(_command())
    return snapshot, reader, product_reader


def test_capture_valid_selected_scenario_creates_existing_snapshot_contract() -> None:
    snapshot, reader, product_reader = _execute(_source())

    assert reader.calls == [("scenario-1", 1)]
    assert isinstance(snapshot, QuotationScenarioSnapshot)
    assert snapshot.review_id == "review-1"
    assert snapshot.decision_id == "decision-1"
    assert snapshot.decision_version == 2
    assert snapshot.currency == "TRY"
    assert snapshot.customer_id == 20
    assert snapshot.opportunity_id == 30
    assert product_reader.calls == [(101, 102)]


def test_capture_preserves_source_values_and_order() -> None:
    snapshot, _, _ = _execute(_source())

    assert [line.line_id for line in snapshot.lines] == ["line-2", "line-1"]
    assert snapshot.lines[0].sales_unit_price == Decimal("0")
    assert snapshot.lines[0].cost_unit_price == Decimal("4.50")
    assert snapshot.lines[0].product_variant_id == 102
    assert snapshot.lines[1].sales_unit_price == Decimal("10.25")
    assert snapshot.lines[1].cost_unit_price is None


def test_capture_rejects_unselected_scenario() -> None:
    with pytest.raises(WorkbenchContractError):
        _execute(_source(selected=False))


def test_capture_rejects_source_company_mismatch() -> None:
    reader = FakeReader(_source(company_id=2))
    product_reader = FakeProductVariantReader()

    with pytest.raises(WorkbenchContractError):
        CaptureQuotationScenarioUseCase(
            source_reader=reader,
            product_variant_reader=product_reader,
        ).execute(_command())

    assert product_reader.calls == []


def test_capture_validates_all_product_ids_in_one_batch() -> None:
    _, _, product_reader = _execute(_source())

    assert product_reader.calls == [(101, 102)]


def test_capture_validates_repeated_product_id_once() -> None:
    lines = (
        QuotationScenarioSourceLine(
            line_id="line-a",
            product_variant_id=101,
            quantity=Decimal("1"),
            sales_unit_price=Decimal("3"),
            cost_unit_price=None,
            description=None,
            uom_id=None,
            sequence=None,
        ),
        QuotationScenarioSourceLine(
            line_id="line-b",
            product_variant_id=101,
            quantity=Decimal("2"),
            sales_unit_price=Decimal("4"),
            cost_unit_price=None,
            description=None,
            uom_id=None,
            sequence=None,
        ),
    )
    _, _, product_reader = _execute(_source(lines=lines), products=(Product(101),))

    assert product_reader.calls == [(101,)]


def test_capture_rejects_missing_product_product_variant() -> None:
    with pytest.raises(WorkbenchContractError):
        _execute(_source(), products=(Product(101),))


@pytest.mark.parametrize(
    "products",
    [
        (Product(101), Product(101), Product(102)),
        (Product(101), Product(102), Product(999)),
    ],
)
def test_capture_rejects_unexpected_or_duplicate_product_identity(products: Sequence[Product]) -> None:
    with pytest.raises(WorkbenchContractError):
        _execute(_source(), products=products)


def test_capture_rejects_incompatible_product_company() -> None:
    with pytest.raises(WorkbenchContractError):
        _execute(_source(company_id=1), products=(Product(101, company_id=1), Product(102, company_id=2)))


def test_capture_accepts_shared_product_company() -> None:
    snapshot, _, _ = _execute(_source(company_id=1), products=(Product(101, company_id=None), Product(102)))

    assert snapshot.company_id == 1


def test_capture_accepts_active_products() -> None:
    snapshot, _, _ = _execute(_source(), products=(Product(101, active=True), Product(102, active=True)))

    assert [line.product_variant_id for line in snapshot.lines] == [102, 101]


def test_capture_rejects_inactive_products_matching_existing_active_product_policy() -> None:
    with pytest.raises(WorkbenchContractError, match="active"):
        _execute(_source(), products=(Product(101), Product(102, active=False)))


def test_capture_is_deterministic_for_repeated_capture() -> None:
    source = _source()
    first, _, _ = _execute(source)
    second, _, _ = _execute(source)

    assert first == second
