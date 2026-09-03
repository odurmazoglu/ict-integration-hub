from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.application.quotation import (
    PersistQuotationScenarioEvidenceUseCase,
    QuotationEvidenceConflictError,
    QuotationEvidenceDataIntegrityError,
    QuotationEvidenceNotFoundError,
    QuotationScenarioLine,
    QuotationScenarioSnapshot,
)
from app.application.workbench.exceptions import WorkbenchContractError
from app.db.base import Base
from app.models.quotation_scenario_evidence import QuotationScenarioEvidence
from app.persistence.quotation_scenario_evidence_repository import (
    SqlAlchemyQuotationScenarioEvidenceRepository,
    deserialize_quotation_scenario_snapshot,
    serialize_quotation_scenario_snapshot,
)


def _line(
    line_id: str = "line-1",
    *,
    product_variant_id: int = 10,
    quantity: str = "2",
    sales_unit_price: str = "10.00",
    cost_unit_price: str | None = "6.00",
    description: str | None = "Product",
    uom_id: int | None = 1,
) -> QuotationScenarioLine:
    return QuotationScenarioLine(
        line_id=line_id,
        product_variant_id=product_variant_id,
        quantity=Decimal(quantity),
        sales_unit_price=Decimal(sales_unit_price),
        cost_unit_price=None if cost_unit_price is None else Decimal(cost_unit_price),
        description=description,
        uom_id=uom_id,
    )


def _snapshot(*lines: QuotationScenarioLine, **overrides: object) -> QuotationScenarioSnapshot:
    values: dict[str, object] = {
        "scenario_id": "scenario-a",
        "scenario_name": "Scenario A",
        "company_id": 7,
        "customer_id": 501,
        "currency": "eur",
        "lines": lines or (_line(),),
        "review_id": "review-1",
        "decision_id": "decision-1",
        "decision_version": 2,
    }
    values.update(overrides)
    return QuotationScenarioSnapshot(**values)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[QuotationScenarioEvidence.__table__])
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


@pytest.fixture()
def repository(session: Session) -> SqlAlchemyQuotationScenarioEvidenceRepository:
    return SqlAlchemyQuotationScenarioEvidenceRepository(session)


def test_valid_snapshot_persists(
    session: Session,
    repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    stored = repository.persist(_snapshot())

    record = session.scalar(select(QuotationScenarioEvidence))
    assert record is not None
    assert record.company_id == 7
    assert record.review_id == "review-1"
    assert record.decision_id == "decision-1"
    assert record.decision_version == 2
    assert record.scenario_id == "scenario-a"
    assert record.schema_version == 1
    assert record.scenario_snapshot["currency"] == "EUR"
    assert stored == _snapshot()


def test_persisted_snapshot_reconstructs_equivalently(
    repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    original = _snapshot(_line("line-1"), _line("line-2", sales_unit_price="12.345"))
    repository.persist(original)

    loaded = repository.get(
        company_id=7,
        review_id="review-1",
        decision_id="decision-1",
        decision_version=2,
        scenario_id="scenario-a",
    )

    assert loaded == original


def test_use_case_delegates_to_repository(
    repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    use_case = PersistQuotationScenarioEvidenceUseCase(repository=repository)

    stored = use_case.execute(_snapshot())

    assert stored == _snapshot()


def test_exact_replay_is_idempotent(
    session: Session,
    repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    first = repository.persist(_snapshot())
    second = repository.persist(_snapshot())

    assert second == first
    assert session.query(QuotationScenarioEvidence).count() == 1


def test_conflicting_replay_fails_closed(
    session: Session,
    repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    repository.persist(_snapshot())

    with pytest.raises(QuotationEvidenceConflictError):
        repository.persist(_snapshot(_line(sales_unit_price="99.00")))

    record = session.scalar(select(QuotationScenarioEvidence))
    assert record is not None
    assert record.scenario_snapshot["lines"][0]["sales_unit_price"] == "10.00"


def test_different_scenario_id_under_same_decision_persists_independently(
    session: Session,
    repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    repository.persist(_snapshot(scenario_id="scenario-a"))
    repository.persist(_snapshot(scenario_id="scenario-b"))
    repository.persist(_snapshot(scenario_id="scenario-c"))

    assert session.query(QuotationScenarioEvidence).count() == 3


def test_different_decision_version_persists_independently(
    session: Session,
    repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    repository.persist(_snapshot(decision_version=2))
    repository.persist(_snapshot(decision_version=3))

    assert session.query(QuotationScenarioEvidence).count() == 2


def test_line_order_is_preserved_on_reconstruction(
    repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    original = _snapshot(_line("line-a"), _line("line-b"), _line("line-c"))
    repository.persist(original)

    loaded = repository.get(
        company_id=7,
        review_id="review-1",
        decision_id="decision-1",
        decision_version=2,
        scenario_id="scenario-a",
    )

    assert tuple(line.line_id for line in loaded.lines) == ("line-a", "line-b", "line-c")


def test_changed_line_order_conflicts(
    repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    first = _line("line-a")
    second = _line("line-b")
    repository.persist(_snapshot(first, second))

    with pytest.raises(QuotationEvidenceConflictError):
        repository.persist(_snapshot(second, first))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("quantity", "3.000"),
        ("sales_unit_price", "123.4500"),
        ("cost_unit_price", "0.010000"),
    ),
)
def test_decimal_values_round_trip_exactly(
    repository: SqlAlchemyQuotationScenarioEvidenceRepository,
    field: str,
    value: str,
) -> None:
    original = _snapshot(_line(**{field: value}))
    repository.persist(original)

    loaded = repository.get(
        company_id=7,
        review_id="review-1",
        decision_id="decision-1",
        decision_version=2,
        scenario_id="scenario-a",
    )

    assert getattr(loaded.lines[0], field) == Decimal(value)


def test_zero_sales_price_is_preserved(
    session: Session,
    repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    repository.persist(_snapshot(_line(sales_unit_price="0")))

    record = session.scalar(select(QuotationScenarioEvidence))
    assert record is not None
    assert record.scenario_snapshot["lines"][0]["sales_unit_price"] == "0"
    loaded = repository.get(
        company_id=7,
        review_id="review-1",
        decision_id="decision-1",
        decision_version=2,
        scenario_id="scenario-a",
    )
    assert loaded.lines[0].sales_unit_price == Decimal("0")


@pytest.mark.parametrize(
    "overrides",
    (
        {"opportunity_id": 4321},
        {"opportunity_id": None},
    ),
)
def test_optional_opportunity_is_preserved(
    repository: SqlAlchemyQuotationScenarioEvidenceRepository,
    overrides: dict[str, object],
) -> None:
    original = _snapshot(**overrides)
    repository.persist(original)

    loaded = repository.get(
        company_id=7,
        review_id="review-1",
        decision_id="decision-1",
        decision_version=2,
        scenario_id="scenario-a",
    )

    assert loaded.opportunity_id == overrides["opportunity_id"]


@pytest.mark.parametrize(
    ("cost_unit_price", "uom_id", "description"),
    (
        (None, None, None),
        (None, 5, ""),
        ("6.00", None, "kept"),
    ),
)
def test_optional_line_fields_are_preserved(
    repository: SqlAlchemyQuotationScenarioEvidenceRepository,
    cost_unit_price: str | None,
    uom_id: int | None,
    description: str | None,
) -> None:
    original = _snapshot(
        _line(cost_unit_price=cost_unit_price, uom_id=uom_id, description=description),
    )
    repository.persist(original)

    loaded = repository.get(
        company_id=7,
        review_id="review-1",
        decision_id="decision-1",
        decision_version=2,
        scenario_id="scenario-a",
    )

    line = loaded.lines[0]
    assert line.cost_unit_price == (None if cost_unit_price is None else Decimal(cost_unit_price))
    assert line.uom_id == uom_id
    assert line.description == description


@pytest.mark.parametrize(
    "changed",
    (
        {"lines": (_line(sales_unit_price="10.01"),)},
        {"customer_id": 999},
        {"currency": "usd"},
        {"scenario_name": "Renamed Scenario"},
    ),
)
def test_changed_commercial_snapshot_conflicts(
    repository: SqlAlchemyQuotationScenarioEvidenceRepository,
    changed: dict[str, object],
) -> None:
    repository.persist(_snapshot())

    with pytest.raises(QuotationEvidenceConflictError):
        repository.persist(_snapshot(**changed))


def test_duplicate_semantic_identity_is_protected_by_db_constraint(session: Session) -> None:
    payload = serialize_quotation_scenario_snapshot(_snapshot())
    session.add(
        QuotationScenarioEvidence(
            company_id=7,
            review_id="review-1",
            decision_id="decision-1",
            decision_version=2,
            scenario_id="scenario-a",
            schema_version=1,
            scenario_snapshot=payload,
        )
    )
    session.add(
        QuotationScenarioEvidence(
            company_id=7,
            review_id="review-1",
            decision_id="decision-1",
            decision_version=2,
            scenario_id="scenario-a",
            schema_version=1,
            scenario_snapshot=payload,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_duplicate_line_identity_in_snapshot_fails_closed() -> None:
    with pytest.raises(WorkbenchContractError):
        _snapshot(_line("dup"), _line("dup"))


def test_tampered_stored_payload_fails_closed_on_load(session: Session) -> None:
    payload = serialize_quotation_scenario_snapshot(_snapshot(_line("line-a"), _line("line-b")))
    payload["lines"][1]["line_id"] = "line-a"
    session.add(
        QuotationScenarioEvidence(
            company_id=7,
            review_id="review-1",
            decision_id="decision-1",
            decision_version=2,
            scenario_id="scenario-a",
            schema_version=1,
            scenario_snapshot=payload,
        )
    )
    session.flush()

    with pytest.raises(QuotationEvidenceDataIntegrityError):
        SqlAlchemyQuotationScenarioEvidenceRepository(session).get(
            company_id=7,
            review_id="review-1",
            decision_id="decision-1",
            decision_version=2,
            scenario_id="scenario-a",
        )


def test_float_decimal_payload_is_rejected_on_deserialize() -> None:
    payload = serialize_quotation_scenario_snapshot(_snapshot())
    payload["lines"][0]["sales_unit_price"] = 10.0

    with pytest.raises(QuotationEvidenceDataIntegrityError):
        deserialize_quotation_scenario_snapshot(payload)


def test_get_missing_evidence_fails_closed(
    repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    with pytest.raises(QuotationEvidenceNotFoundError):
        repository.get(
            company_id=7,
            review_id="review-1",
            decision_id="decision-1",
            decision_version=2,
            scenario_id="missing",
        )


def test_concurrent_duplicate_insertion_recovers_idempotently(
    session: Session,
    repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    repository.persist(_snapshot())
    _blind_first_lookup(repository)

    replayed = repository.persist(_snapshot())

    assert replayed == _snapshot()
    assert session.query(QuotationScenarioEvidence).count() == 1


def test_concurrent_duplicate_insertion_with_changed_snapshot_conflicts(
    session: Session,
    repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    repository.persist(_snapshot())
    _blind_first_lookup(repository)

    with pytest.raises(QuotationEvidenceConflictError):
        repository.persist(_snapshot(_line(sales_unit_price="77.00")))

    assert session.query(QuotationScenarioEvidence).count() == 1


def test_application_evidence_port_has_no_persistence_leak() -> None:
    from pathlib import Path

    source = Path("app/application/quotation/evidence.py").read_text(encoding="utf-8").lower()

    assert "sqlalchemy" not in source
    assert "app.models" not in source
    assert "session" not in source


def _blind_first_lookup(repository: SqlAlchemyQuotationScenarioEvidenceRepository) -> None:
    """Force the next semantic lookup to miss so the DB unique constraint fires."""

    original = repository._find
    state = {"used": False}

    def _find(snapshot: QuotationScenarioSnapshot):
        if not state["used"]:
            state["used"] = True
            return None
        return original(snapshot)

    repository._find = _find  # type: ignore[method-assign]
