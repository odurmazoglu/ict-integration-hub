from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.core.config import get_settings
from app.models.workbench_review_write_authorization import WorkbenchReviewWriteAuthorization


def test_write_authorization_upgrade_downgrade_and_metadata_contract(tmp_path: Path, monkeypatch) -> None:
    url = f"sqlite:///{tmp_path / 'authorization-migration.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    engine = create_engine(url)
    try:
        command.upgrade(config, "202607170027")
        assert WorkbenchReviewWriteAuthorization.__tablename__ not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        inspector = inspect(engine)
        name = WorkbenchReviewWriteAuthorization.__tablename__
        assert {c["name"] for c in inspector.get_columns(name)} == set(
            WorkbenchReviewWriteAuthorization.__table__.columns.keys()
        )
        assert {c["name"] for c in inspector.get_check_constraints(name)} == {
            c.name
            for c in WorkbenchReviewWriteAuthorization.__table__.constraints
            if c.__class__.__name__ == "CheckConstraint"
        }
        assert {i["name"] for i in inspector.get_indexes(name)} == {
            i.name for i in WorkbenchReviewWriteAuthorization.__table__.indexes
        }
        with engine.connect() as connection:
            # P0-PROD-15T and P0-PROD-18E-2 each added one more, unrelated migration on
            # top -- "head" now lands two revisions further than when this test was written.
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "202607170032"
        _assert_operation_type_check_constraint(engine, name)
        # Rows using an operation type must be cleared before downgrading past the
        # migration that introduced it -- SQLite's batch-recreate (and PostgreSQL's
        # default ADD CONSTRAINT validation) both re-validate existing rows against
        # the restored, narrower constraint, exactly as for any other constraint
        # tightening.
        _delete_rows_with_operation_type(engine, name, "CREATE_NEW_PRODUCT")
        command.downgrade(config, "202607170029")
        _assert_pre_09g_check_constraint_rejects_create_new_product(engine, name)
        for operation_type in ("CREATE_PERMANENT_SUPPLIER", "ONE_OFF_VENDOR_SUPPLIER", "ONE_OFF_VENDOR_ARCHIVE"):
            _delete_rows_with_operation_type(engine, name, operation_type)
        command.downgrade(config, "202607170028")
        _assert_pre_09f_check_constraint_rejects_new_operation_types(engine, name)
        command.downgrade(config, "202607170027")
        assert name not in inspect(engine).get_table_names()
        assert "workbench_review_decisions" in inspect(engine).get_table_names()
        assert "execution_source_invoice_evidence" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
        get_settings.cache_clear()


def _insert_authorization(engine, table_name: str, *, review_id: str, operation_type: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                INSERT INTO {table_name}
                    (authorization_id, company_id, review_id, operation_type, target_version,
                     status, authorized_by, expires_at)
                VALUES (:auth_id, 1, :review_id, :operation_type, 1, 'pending', 'tester',
                        datetime('now', '+15 minutes'))
                """
            ),
            {"auth_id": f"auth-{operation_type}-{review_id}", "review_id": review_id, "operation_type": operation_type},
        )


def _seed_review(engine, review_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO workbench_review_items
                    (review_id, company_id, invoice_id, invoice_number, supplier_tax_number, supplier_name,
                     invoice_date, currency, total_amount, workflow, status, review_reasons, warnings,
                     version, idempotency_key)
                VALUES (:review_id, 1, 'inv', 'INV-1', '1234567890', 'Vendor', '2026-01-01', 'TRY', 1.0,
                        'manual_review', 'pending_review', '[]', '[]', 1, :review_id)
                """
            ),
            {"review_id": review_id},
        )


def _delete_rows_with_operation_type(engine, table_name: str, operation_type: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(f"DELETE FROM {table_name} WHERE operation_type = :operation_type"),
            {"operation_type": operation_type},
        )


def _assert_operation_type_check_constraint(engine, table_name: str) -> None:
    """P0-PROD-09F/09G: the extended check constraint accepts all five operation
    types and still rejects anything else -- proving the migrations actually
    widened the constraint rather than merely reordering its name/columns."""

    _seed_review(engine, "review-ck-check")
    for operation_type in (
        "EXECUTE_VENDOR_BILL",
        "CREATE_PERMANENT_SUPPLIER",
        "ONE_OFF_VENDOR_SUPPLIER",
        "ONE_OFF_VENDOR_ARCHIVE",
        "CREATE_NEW_PRODUCT",
    ):
        _insert_authorization(engine, table_name, review_id="review-ck-check", operation_type=operation_type)
    with pytest.raises(IntegrityError):
        _insert_authorization(engine, table_name, review_id="review-ck-check", operation_type="SOMETHING_ELSE")


def _assert_pre_09g_check_constraint_rejects_create_new_product(engine, table_name: str) -> None:
    """After downgrading to 202607170029, the pre-09G constraint is restored exactly
    -- CREATE_NEW_PRODUCT is rejected again while the 09F operation types are still
    accepted, proving downgrade reverts only what this one migration added."""

    _seed_review(engine, "review-ck-downgrade-09g")
    _insert_authorization(
        engine, table_name, review_id="review-ck-downgrade-09g", operation_type="ONE_OFF_VENDOR_ARCHIVE"
    )
    with pytest.raises(IntegrityError):
        _insert_authorization(
            engine, table_name, review_id="review-ck-downgrade-09g", operation_type="CREATE_NEW_PRODUCT"
        )


def _assert_pre_09f_check_constraint_rejects_new_operation_types(engine, table_name: str) -> None:
    """After downgrading to 202607170028, the pre-09F constraint is restored exactly
    -- the new operation types are rejected again, proving downgrade genuinely
    reverts the constraint body, not just the alembic_version pointer."""

    _seed_review(engine, "review-ck-downgrade")
    _insert_authorization(engine, table_name, review_id="review-ck-downgrade", operation_type="EXECUTE_VENDOR_BILL")
    for operation_type in ("CREATE_PERMANENT_SUPPLIER", "ONE_OFF_VENDOR_SUPPLIER", "ONE_OFF_VENDOR_ARCHIVE"):
        with pytest.raises(IntegrityError):
            _insert_authorization(engine, table_name, review_id="review-ck-downgrade", operation_type=operation_type)
