"""Operating expense mapping foundation: DTO contract + read repository.

This PR adds only the persistent foundation; no classification or execution
behavior is wired. These tests assert the DTO contract and the deterministic
``find_for_supplier`` semantics, including fail-closed on duplicate enabled rows.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.application.expense_mapping import (
    OperatingExpenseMapping,
    OperatingExpenseMappingContractError,
    OperatingExpenseMappingDataIntegrityError,
    OperatingExpenseMappingError,
)
from app.db.base import Base
from app.models.operating_expense_mapping import OperatingExpenseMappingRecord
from app.persistence import SqlAlchemyOperatingExpenseMappingRepository


@pytest.fixture()
def session() -> Session:
    factory = sessionmaker(bind=create_engine("sqlite:///:memory:"))
    with factory() as db_session:
        Base.metadata.create_all(db_session.get_bind())
        yield db_session


def _add(
    session: Session,
    *,
    company_id: int = 1,
    vendor_partner_id: int = 501,
    expense_account_id: int = 9001,
    expense_category: str = "OFFICE_BUILDING_EXPENSE",
    enabled: bool = True,
) -> OperatingExpenseMappingRecord:
    record = OperatingExpenseMappingRecord(
        company_id=company_id,
        vendor_partner_id=vendor_partner_id,
        expense_account_id=expense_account_id,
        expense_category=expense_category,
        enabled=enabled,
    )
    session.add(record)
    session.flush()
    return record


# --------------------------------------------------------------------------- DTO


def test_mapping_dto_is_frozen_and_validated() -> None:
    mapping = OperatingExpenseMapping(
        id=1,
        company_id=1,
        vendor_partner_id=501,
        expense_account_id=9001,
        expense_category="ELECTRICITY",
        enabled=True,
    )

    assert mapping.expense_category == "ELECTRICITY"
    with pytest.raises(FrozenInstanceError):
        mapping.enabled = False  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": 0},
        {"company_id": 0},
        {"company_id": -1},
        {"vendor_partner_id": 0},
        {"expense_account_id": 0},
        {"expense_category": ""},
        {"expense_category": "  "},
        {"expense_category": "lowercase"},
        {"expense_category": "1LEADING_DIGIT"},
        {"enabled": 1},
    ],
)
def test_mapping_dto_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    base: dict[str, object] = {
        "id": 1,
        "company_id": 1,
        "vendor_partner_id": 501,
        "expense_account_id": 9001,
        "expense_category": "ELECTRICITY",
        "enabled": True,
    }
    base.update(overrides)
    with pytest.raises(OperatingExpenseMappingContractError):
        OperatingExpenseMapping(**base)  # type: ignore[arg-type]


# -------------------------------------------------------------------- repository


def test_repository_returns_exact_enabled_mapping(session: Session) -> None:
    record = _add(session)

    result = SqlAlchemyOperatingExpenseMappingRepository(session).find_for_supplier(company_id=1, vendor_partner_id=501)

    assert isinstance(result, OperatingExpenseMapping)
    assert result.id == record.id
    assert result.company_id == 1
    assert result.vendor_partner_id == 501
    assert result.expense_account_id == 9001
    assert result.expense_category == "OFFICE_BUILDING_EXPENSE"
    assert result.enabled is True


def test_repository_ignores_disabled_mapping(session: Session) -> None:
    _add(session, enabled=False)

    result = SqlAlchemyOperatingExpenseMappingRepository(session).find_for_supplier(company_id=1, vendor_partner_id=501)

    assert result is None


def test_repository_ignores_other_company(session: Session) -> None:
    _add(session, company_id=2)

    result = SqlAlchemyOperatingExpenseMappingRepository(session).find_for_supplier(company_id=1, vendor_partner_id=501)

    assert result is None


def test_repository_ignores_other_vendor_partner(session: Session) -> None:
    _add(session, vendor_partner_id=999)

    result = SqlAlchemyOperatingExpenseMappingRepository(session).find_for_supplier(company_id=1, vendor_partner_id=501)

    assert result is None


def test_repository_returns_none_when_no_mapping(session: Session) -> None:
    result = SqlAlchemyOperatingExpenseMappingRepository(session).find_for_supplier(company_id=1, vendor_partner_id=501)

    assert result is None


def test_repository_allows_disabled_history_alongside_enabled(session: Session) -> None:
    _add(session, expense_account_id=8000, enabled=False)
    _add(session, expense_account_id=9001, enabled=True)

    result = SqlAlchemyOperatingExpenseMappingRepository(session).find_for_supplier(company_id=1, vendor_partner_id=501)

    assert result is not None
    assert result.expense_account_id == 9001


@pytest.mark.parametrize(("company_id", "vendor_partner_id"), [(0, 501), (1, 0), (-1, 501), (1, -2)])
def test_repository_rejects_non_positive_query_arguments(
    session: Session, company_id: int, vendor_partner_id: int
) -> None:
    with pytest.raises(OperatingExpenseMappingError):
        SqlAlchemyOperatingExpenseMappingRepository(session).find_for_supplier(
            company_id=company_id, vendor_partner_id=vendor_partner_id
        )


def test_partial_unique_index_blocks_two_enabled_rows(session: Session) -> None:
    _add(session, enabled=True)
    with pytest.raises(IntegrityError):
        _add(session, expense_account_id=9002, enabled=True)


def test_repository_fails_closed_when_exposed_to_two_enabled_rows() -> None:
    """Defense in depth: the DB blocks this state, but the reader still refuses to guess."""

    row_a = OperatingExpenseMappingRecord(
        id=1,
        company_id=1,
        vendor_partner_id=501,
        expense_account_id=9001,
        expense_category="ELECTRICITY",
        enabled=True,
    )
    row_b = OperatingExpenseMappingRecord(
        id=2,
        company_id=1,
        vendor_partner_id=501,
        expense_account_id=9002,
        expense_category="ELECTRICITY",
        enabled=True,
    )

    class _ScalarResult:
        def all(self) -> list[OperatingExpenseMappingRecord]:
            return [row_a, row_b]

    class _TwoEnabledRowsSession:
        def scalars(self, *_args: object, **_kwargs: object) -> _ScalarResult:
            return _ScalarResult()

    repository = SqlAlchemyOperatingExpenseMappingRepository(_TwoEnabledRowsSession())  # type: ignore[arg-type]

    with pytest.raises(OperatingExpenseMappingDataIntegrityError):
        repository.find_for_supplier(company_id=1, vendor_partner_id=501)
