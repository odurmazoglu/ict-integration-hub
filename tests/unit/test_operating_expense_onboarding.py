"""Controlled, idempotent operating-expense mapping onboarding (P0-3C6 / PR 5).

No live Odoo. The operator supplies the approved expense account; it is never inferred.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.application.expense_mapping import (
    OnboardOperatingExpenseMappingCommand,
    OnboardOperatingExpenseMappingUseCase,
    OperatingExpenseMappingConflictError,
    OperatingExpenseMappingContractError,
    OperatingExpenseMappingError,
    OperatingExpenseMappingOnboardingOutcome,
)
from app.db.base import Base
from app.models.operating_expense_mapping import OperatingExpenseMappingRecord
from app.persistence import SqlAlchemyOperatingExpenseMappingRepository

COMPANY_ID = 1
PARTNER_ID = 101
ACCOUNT_ID = 9001
CATEGORY = "OFFICE_OPERATING_EXPENSE"


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


def _use_case(session: Session) -> OnboardOperatingExpenseMappingUseCase:
    return OnboardOperatingExpenseMappingUseCase(SqlAlchemyOperatingExpenseMappingRepository(session))


def _command(**overrides) -> OnboardOperatingExpenseMappingCommand:
    base = {
        "company_id": COMPANY_ID,
        "vendor_partner_id": PARTNER_ID,
        "expense_account_id": ACCOUNT_ID,
        "expense_category": CATEGORY,
        "enabled": True,
    }
    base.update(overrides)
    return OnboardOperatingExpenseMappingCommand(**base)


# --------------------------------------------------------------------------- use case


def test_create_new_mapping(session: Session) -> None:
    result = _use_case(session).execute(_command())

    assert result.outcome is OperatingExpenseMappingOnboardingOutcome.CREATED
    assert result.mapping.company_id == COMPANY_ID
    assert result.mapping.vendor_partner_id == PARTNER_ID
    assert result.mapping.expense_account_id == ACCOUNT_ID
    assert result.mapping.expense_category == CATEGORY
    assert result.mapping.enabled is True


def test_identical_request_is_idempotent_noop(session: Session) -> None:
    _use_case(session).execute(_command())
    result = _use_case(session).execute(_command())

    assert result.outcome is OperatingExpenseMappingOnboardingOutcome.ALREADY_CONFIGURED
    assert session.execute(select(OperatingExpenseMappingRecord)).scalars().all().__len__() == 1


def test_conflicting_account_fails_closed(session: Session) -> None:
    _use_case(session).execute(_command())
    with pytest.raises(OperatingExpenseMappingConflictError):
        _use_case(session).execute(_command(expense_account_id=9002))
    assert session.execute(select(OperatingExpenseMappingRecord)).scalar_one().expense_account_id == ACCOUNT_ID


def test_conflicting_category_fails_closed(session: Session) -> None:
    _use_case(session).execute(_command())
    with pytest.raises(OperatingExpenseMappingConflictError):
        _use_case(session).execute(_command(expense_category="ELECTRICITY"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"company_id": 0},
        {"company_id": -1},
        {"vendor_partner_id": 0},
        {"expense_account_id": 0},
        {"expense_account_id": -5},
    ],
)
def test_non_positive_ids_rejected(session: Session, overrides: dict) -> None:
    with pytest.raises(OperatingExpenseMappingError):
        _use_case(session).execute(_command(**overrides))


@pytest.mark.parametrize("category", ["", "   ", "lowercase", "1STARTS_DIGIT"])
def test_bad_category_rejected(session: Session, category: str) -> None:
    with pytest.raises(OperatingExpenseMappingError):
        _use_case(session).execute(_command(expense_category=category))


def test_bool_ids_rejected(session: Session) -> None:
    with pytest.raises(OperatingExpenseMappingError):
        _use_case(session).execute(_command(company_id=True))  # noqa: FBT003


def test_company_isolation_preserved(session: Session) -> None:
    _use_case(session).execute(_command(company_id=1))
    result = _use_case(session).execute(_command(company_id=2))

    assert result.outcome is OperatingExpenseMappingOnboardingOutcome.CREATED
    rows = session.execute(select(OperatingExpenseMappingRecord)).scalars().all()
    assert {r.company_id for r in rows} == {1, 2}


def test_disabled_historical_mapping_coexists_with_enabled(session: Session) -> None:
    _use_case(session).execute(_command(enabled=False))
    result = _use_case(session).execute(_command(enabled=True))  # find_for_supplier ignores the disabled row

    assert result.outcome is OperatingExpenseMappingOnboardingOutcome.CREATED
    rows = session.execute(select(OperatingExpenseMappingRecord)).scalars().all()
    assert sorted(r.enabled for r in rows) == [False, True]


# --------------------------------------------------------------------------- repository create()


def test_repository_create_blocks_two_enabled_rows(session: Session) -> None:
    repo = SqlAlchemyOperatingExpenseMappingRepository(session)
    repo.create(
        company_id=COMPANY_ID,
        vendor_partner_id=PARTNER_ID,
        expense_account_id=ACCOUNT_ID,
        expense_category=CATEGORY,
        enabled=True,
    )
    with pytest.raises(Exception) as excinfo:
        repo.create(
            company_id=COMPANY_ID,
            vendor_partner_id=PARTNER_ID,
            expense_account_id=9002,
            expense_category=CATEGORY,
            enabled=True,
        )
    assert not isinstance(excinfo.value, IntegrityError)  # translated to a safe domain error


def test_repository_create_rejects_bad_category(session: Session) -> None:
    with pytest.raises(OperatingExpenseMappingContractError):
        SqlAlchemyOperatingExpenseMappingRepository(session).create(
            company_id=COMPANY_ID,
            vendor_partner_id=PARTNER_ID,
            expense_account_id=ACCOUNT_ID,
            expense_category="lowercase",
            enabled=True,
        )


# --------------------------------------------------------------------------- script wiring


def test_onboarding_script_refuses_without_confirm(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import scripts.onboard_operating_expense_mapping as script

    monkeypatch.setattr(
        "sys.argv",
        [
            "onboard",
            "--company-id",
            "1",
            "--vendor-partner-id",
            "101",
            "--expense-account-id",
            "9001",
            "--expense-category",
            "OFFICE_OPERATING_EXPENSE",
        ],
    )
    rc = script.main()
    assert rc == 2
    assert "refused" in capsys.readouterr().out
