"""Migration 202607170035: fixed-asset accounting resolution + frozen evidence columns.

upgrade -> downgrade -> upgrade on a real (SQLite) Alembic run, with a historical
``expense_account`` resolution created *before* the migration that must survive every
step byte-identically, and a downgrade that refuses to drop fixed-asset evidence.
Also run against PostgreSQL by ``test_postgres_migration_compatibility`` when
``POSTGRES_TEST_DATABASE_URL`` is set.
"""

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.core.config import get_settings
from app.models.workbench_review_accounting_resolution import WorkbenchReviewAccountingResolution

TABLE = "workbench_review_accounting_resolutions"
EVIDENCE_TABLES = ("workbench_review_execution_evidence", "execution_source_invoice_evidence")


def _seed_review(connection) -> None:
    connection.execute(
        text(
            "INSERT INTO workbench_review_items (review_id, company_id, invoice_id, invoice_number, "
            "supplier_tax_number, supplier_name, invoice_date, currency, total_amount, workflow, status, "
            "review_reasons, warnings, version, idempotency_key) VALUES ('review:hist', 1, 'e', 'n', 'v', 's', "
            "'2026-09-08', 'TRY', 1, 'manual_review', 'pending_review', '[]', '[]', 1, 'k')"
        )
    )


def _historical_row(connection) -> tuple:
    return connection.execute(
        text(
            f"SELECT review_id, company_id, review_version, treatment_type, expense_account_id, expense_category "
            f"FROM {TABLE}"
        )
    ).one()


def test_fixed_asset_migration_upgrade_downgrade_upgrade(tmp_path: Path, monkeypatch) -> None:
    url = f"sqlite:///{tmp_path / 'fixed-asset-migration.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    engine = create_engine(url)
    try:
        command.upgrade(config, "202607170034")
        with engine.begin() as connection:
            _seed_review(connection)
            connection.execute(
                text(
                    f"INSERT INTO {TABLE} (review_id, company_id, review_version, treatment_type, "
                    "expense_account_id, expense_category) VALUES ('review:hist', 1, 1, 'expense_account', 247, "
                    "'IT_HARDWARE_INTERNAL')"
                )
            )
            historical = _historical_row(connection)

        command.upgrade(config, "202607170035")  # this migration; later heads are tested separately
        inspector = inspect(engine)
        assert {c["name"] for c in inspector.get_columns(TABLE)} == set(
            WorkbenchReviewAccountingResolution.__table__.columns.keys()
        )
        assert {c["name"] for c in inspector.get_check_constraints(TABLE)} == {
            c.name
            for c in WorkbenchReviewAccountingResolution.__table__.constraints
            if c.__class__.__name__ == "CheckConstraint"
        }
        for table in EVIDENCE_TABLES:
            assert "fixed_asset_accounting" in {c["name"] for c in inspector.get_columns(table)}
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "202607170035"
            assert _historical_row(connection) == historical  # never rewritten
            assert connection.execute(text(f"SELECT asset_account_id, depreciation_model_id FROM {TABLE}")).one() == (
                None,
                None,
            )

        command.downgrade(config, "202607170034")
        with engine.begin() as connection:
            assert _historical_row(connection) == historical
        for table in EVIDENCE_TABLES:
            assert "fixed_asset_accounting" not in {c["name"] for c in inspect(engine).get_columns(table)}

        command.upgrade(config, "202607170035")  # this migration; later heads are tested separately
        with engine.begin() as connection:
            assert _historical_row(connection) == historical
            connection.execute(
                text(
                    f"INSERT INTO {TABLE} (review_id, company_id, review_version, treatment_type, "
                    "asset_account_id, depreciation_model_id) VALUES ('review:hist', 1, 2, 'capitalize_fixed_asset', "
                    "74, 3)"
                )
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(  # mixed shape is rejected by the database itself
                text(
                    f"INSERT INTO {TABLE} (review_id, company_id, review_version, treatment_type, "
                    "asset_account_id, depreciation_model_id, expense_account_id, expense_category) VALUES "
                    "('review:hist', 1, 3, 'capitalize_fixed_asset', 74, 3, 247, 'X')"
                )
            )
        with pytest.raises(RuntimeError, match="Refusing to downgrade"):
            command.downgrade(config, "202607170034")
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "202607170035"
    finally:
        engine.dispose()
        get_settings.cache_clear()
