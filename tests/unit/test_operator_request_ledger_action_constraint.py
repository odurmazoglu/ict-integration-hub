"""Operator request ledger action constraint (#208 ``product_mapping``, PR C ``product_line_mapping``).

The ledger's ck_workbench_operator_requests_action must accept every operator action
the poller can parse, and the application enum, the Odoo label mapping, the ORM model
and the migrated database must not drift apart again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.application.workbench.accounting_resolution import AccountingTreatmentType
from app.application.workbench.operator_request_ingestion import (
    OperatorRequest,
    OperatorRequestAction,
    operator_request_key,
)
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workbench.supplier_resolution import SupplierResolutionMode
from app.core.config import get_settings
from app.db.base import Base
from app.erp.odoo.workbench_operator_request_reader import ACTION_BY_ODOO_VALUE, PARENT_ROW_ACTIONS
from app.models.workbench_operator_request import OPERATOR_REQUEST_ACTIONS, WorkbenchOperatorRequest
from app.persistence.workbench_operator_request_ledger import SqlAlchemyOperatorRequestLedger

APPLICATION_ACTIONS = {action.value for action in OperatorRequestAction}
ACTION_INPUTS: dict[OperatorRequestAction, dict[str, Any]] = {
    OperatorRequestAction.SUPPLIER_RESOLUTION: {
        "supplier_mode": SupplierResolutionMode.MATCH_EXISTING,
        "partner_id": 7,
    },
    OperatorRequestAction.PURCHASE_PURPOSE: {"purchase_purpose": PurchasePurpose.INTERNAL_USE},
    OperatorRequestAction.ACCOUNTING_RESOLUTION: {"treatment_type": AccountingTreatmentType.EXPENSE_ACCOUNT},
    OperatorRequestAction.DECISION: {},
    OperatorRequestAction.EXECUTE_VENDOR_BILL: {},
    OperatorRequestAction.PRODUCT_MAPPING: {"line_number": "1", "product_id": 393},
    OperatorRequestAction.PRODUCT_LINE_MAPPING: {"line_number": "2", "product_id": 394},
}


def _request(action: OperatorRequestAction, record_id: int) -> OperatorRequest:
    return OperatorRequest(
        odoo_record_id=record_id,
        review_id="review:ledger-constraint",
        company_id=1,
        action=action,
        expected_version=2,
        requested_by_odoo_user_id=2,
        requested_at=datetime(2026, 10, 7, 21, 14, 57, tzinfo=UTC),
        **ACTION_INPUTS[action],
    )


# --- the action sets agree ----------------------------------------------------------


def test_every_application_action_has_test_inputs() -> None:
    assert set(ACTION_INPUTS) == set(OperatorRequestAction)


def test_model_action_tuple_equals_the_application_action_enum() -> None:
    assert len(OPERATOR_REQUEST_ACTIONS) == len(set(OPERATOR_REQUEST_ACTIONS))
    assert set(OPERATOR_REQUEST_ACTIONS) == APPLICATION_ACTIONS
    assert "product_mapping" in OPERATOR_REQUEST_ACTIONS


def test_every_parsed_parent_value_is_a_parent_action_and_every_parent_action_is_parseable() -> None:
    assert {action.value for action in ACTION_BY_ODOO_VALUE.values()} == {a.value for a in PARENT_ROW_ACTIONS}
    assert ACTION_BY_ODOO_VALUE["Ürün Eşleştir"] is OperatorRequestAction.PRODUCT_MAPPING
    # PR C: the child line action is only ever built by the child row reader, never parsed from a parent row.
    assert APPLICATION_ACTIONS - {a.value for a in PARENT_ROW_ACTIONS} == {"product_line_mapping"}
    assert "product_line_mapping" not in ACTION_BY_ODOO_VALUE


def test_model_check_constraint_lists_exactly_the_application_actions() -> None:
    [constraint] = [
        c for c in WorkbenchOperatorRequest.__table__.constraints if c.name == "ck_workbench_operator_requests_action"
    ]
    listed = {part.strip().strip("'") for part in str(constraint.sqltext).split("(", 1)[1].rstrip(")").split(",")}
    assert listed == APPLICATION_ACTIONS


# --- ORM schema: the real ledger persists every action ------------------------------


@pytest.fixture
def orm_session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[WorkbenchOperatorRequest.__table__])
    with Session(engine) as session:
        yield session


@pytest.mark.parametrize("action", list(OperatorRequestAction), ids=lambda action: action.value)
def test_ledger_persists_every_supported_action(orm_session: Session, action: OperatorRequestAction) -> None:
    request = _request(action, 24)
    entry = SqlAlchemyOperatorRequestLedger(orm_session).start(
        request_key=operator_request_key(request), request=request, actor="operator"
    )
    orm_session.commit()

    assert entry.request_key == operator_request_key(request)
    assert orm_session.scalar(text("SELECT action FROM workbench_operator_requests")) == action.value


def test_unknown_action_is_still_rejected_by_the_model_constraint(orm_session: Session) -> None:
    with pytest.raises(IntegrityError):
        orm_session.execute(text(_raw_insert("not_an_action")))
        orm_session.commit()


def _raw_insert(action: str, request_key: str = "k-unknown") -> str:
    return (
        "INSERT INTO workbench_operator_requests (request_key, company_id, review_id, odoo_record_id, action, "
        "expected_version, requested_by_odoo_user_id, requested_at, actor, status, attempts) VALUES "
        f"('{request_key}', 1, 'review:x', 24, '{action}', 2, 2, '2026-10-07 21:14:57', 'operator', 'in_progress', 0)"
    )


# --- migrated schema: 202607170037 -> 202607170038 round trip -----------------------


@pytest.fixture
def migrated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    url = f"sqlite:///{tmp_path / 'ledger-action.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_engine(url)
    try:
        yield Config("alembic.ini"), engine
    finally:
        engine.dispose()
        get_settings.cache_clear()


def _version(engine) -> str:
    with engine.connect() as connection:
        return connection.scalar(text("SELECT version_num FROM alembic_version"))


def _insert(engine, action: str, request_key: str) -> None:
    with engine.begin() as connection:
        connection.execute(text(_raw_insert(action, request_key)))


def test_202607170038_widens_202607170037_for_product_mapping(migrated) -> None:
    config, engine = migrated
    command.upgrade(config, "202607170037")
    assert _version(engine) == "202607170037"
    with pytest.raises(IntegrityError):
        _insert(engine, "product_mapping", "k-before")

    command.upgrade(config, "202607170038")
    assert _version(engine) == "202607170038"
    _insert(engine, "product_mapping", "k-after")
    with pytest.raises(IntegrityError):
        _insert(engine, "product_line_mapping", "k-line-before")


def test_head_is_202607170039_which_adds_product_line_mapping(migrated) -> None:
    config, engine = migrated
    command.upgrade(config, "202607170038")
    with pytest.raises(IntegrityError):
        _insert(engine, "product_line_mapping", "k-line-before")

    command.upgrade(config, "head")
    assert _version(engine) == "202607170039"
    _insert(engine, "product_line_mapping", "k-line-after")


def test_migrated_schema_accepts_every_action_and_rejects_unknown(migrated) -> None:
    config, engine = migrated
    command.upgrade(config, "head")

    for index, action in enumerate(sorted(APPLICATION_ACTIONS)):
        _insert(engine, action, f"k-{index}")
    with pytest.raises(IntegrityError):
        _insert(engine, "not_an_action", "k-unknown")

    with engine.connect() as connection:
        stored = {row[0] for row in connection.execute(text("SELECT action FROM workbench_operator_requests"))}
    assert stored == APPLICATION_ACTIONS


def test_real_ledger_records_a_product_mapping_request_on_the_migrated_schema(migrated) -> None:
    config, engine = migrated
    command.upgrade(config, "head")
    request = _request(OperatorRequestAction.PRODUCT_MAPPING, 24)

    with Session(engine) as session:
        SqlAlchemyOperatorRequestLedger(session).start(
            request_key=operator_request_key(request), request=request, actor="operator"
        )
        session.commit()

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT action FROM workbench_operator_requests")) == "product_mapping"


def test_downgrade_refuses_while_product_mapping_rows_exist_and_changes_nothing(migrated) -> None:
    config, engine = migrated
    command.upgrade(config, "202607170038")
    _insert(engine, "product_mapping", "k-pm")
    _insert(engine, "decision", "k-dec")

    with pytest.raises(RuntimeError, match="product_mapping"):
        command.downgrade(config, "202607170037")

    assert _version(engine) == "202607170038"
    with engine.connect() as connection:
        rows = sorted(connection.execute(text("SELECT request_key, action FROM workbench_operator_requests")))
    assert rows == [("k-dec", "decision"), ("k-pm", "product_mapping")]


def test_upgrade_downgrade_upgrade_round_trip_without_product_mapping_rows(migrated) -> None:
    config, engine = migrated
    command.upgrade(config, "head")
    _insert(engine, "decision", "k-dec")

    command.downgrade(config, "202607170037")
    assert _version(engine) == "202607170037"
    with pytest.raises(IntegrityError):
        _insert(engine, "product_mapping", "k-pm")

    command.upgrade(config, "head")
    assert _version(engine) == "202607170039"
    _insert(engine, "product_mapping", "k-pm")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM workbench_operator_requests")) == 2


# --- migrated schema: 202607170038 -> 202607170039 (PR C) ---------------------------


def test_real_ledger_records_a_product_line_mapping_request_on_the_migrated_schema(migrated) -> None:
    config, engine = migrated
    command.upgrade(config, "head")
    request = _request(OperatorRequestAction.PRODUCT_LINE_MAPPING, 21)

    with Session(engine) as session:
        SqlAlchemyOperatorRequestLedger(session).start(
            request_key=operator_request_key(request), request=request, actor="operator"
        )
        session.commit()

    with engine.connect() as connection:
        row = connection.execute(text("SELECT action, odoo_record_id FROM workbench_operator_requests")).one()
    assert tuple(row) == ("product_line_mapping", 21)


def test_0039_downgrade_refuses_while_product_line_mapping_rows_exist_and_changes_nothing(migrated) -> None:
    config, engine = migrated
    command.upgrade(config, "head")
    _insert(engine, "product_line_mapping", "k-pl")
    _insert(engine, "product_mapping", "k-pm")

    with pytest.raises(RuntimeError, match="product_line_mapping"):
        command.downgrade(config, "202607170038")

    assert _version(engine) == "202607170039"
    with engine.connect() as connection:
        rows = sorted(connection.execute(text("SELECT request_key, action FROM workbench_operator_requests")))
    assert rows == [("k-pl", "product_line_mapping"), ("k-pm", "product_mapping")]


def test_0039_round_trip_without_product_line_mapping_rows_keeps_product_mapping(migrated) -> None:
    config, engine = migrated
    command.upgrade(config, "head")
    _insert(engine, "product_mapping", "k-pm")

    command.downgrade(config, "202607170038")
    assert _version(engine) == "202607170038"
    with pytest.raises(IntegrityError):
        _insert(engine, "product_line_mapping", "k-pl")

    command.upgrade(config, "head")
    _insert(engine, "product_line_mapping", "k-pl")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM workbench_operator_requests")) == 2
