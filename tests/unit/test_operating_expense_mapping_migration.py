"""Focused upgrade/downgrade coverage for the operating expense mapping migration."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from app.core.config import get_settings

REVISION = "202607170019"
DOWN_REVISION = "202607170018"
TABLE = "operating_expense_mappings"


def test_operating_expense_mapping_migration_upgrade_and_downgrade(tmp_path: Path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'operating_expense_mapping_migration.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")

    command.upgrade(config, REVISION)
    inspector = inspect(create_engine(database_url))
    assert TABLE in inspector.get_table_names()

    columns = {column["name"]: column for column in inspector.get_columns(TABLE)}
    assert set(columns) == {
        "id",
        "company_id",
        "vendor_partner_id",
        "expense_account_id",
        "expense_category",
        "enabled",
        "created_at",
        "updated_at",
    }
    for required in ("company_id", "vendor_partner_id", "expense_account_id", "expense_category", "enabled"):
        assert columns[required]["nullable"] is False

    check_constraints = {c["name"] for c in inspector.get_check_constraints(TABLE)}
    assert {
        "ck_operating_expense_mappings_company_id_positive",
        "ck_operating_expense_mappings_vendor_partner_id_positive",
        "ck_operating_expense_mappings_expense_account_id_positive",
        "ck_operating_expense_mappings_expense_category_not_empty",
    }.issubset(check_constraints)

    indexes = {index["name"]: index for index in inspector.get_indexes(TABLE)}
    assert "ix_operating_expense_mappings_company_partner" in indexes
    assert indexes["ix_operating_expense_mappings_company_partner"]["column_names"] == [
        "company_id",
        "vendor_partner_id",
    ]
    assert "uq_operating_expense_mappings_active_supplier" in indexes
    assert bool(indexes["uq_operating_expense_mappings_active_supplier"]["unique"]) is True

    command.downgrade(config, DOWN_REVISION)
    inspector = inspect(create_engine(database_url))
    assert TABLE not in inspector.get_table_names()

    command.upgrade(config, "head")
    inspector = inspect(create_engine(database_url))
    assert TABLE in inspector.get_table_names()
    get_settings.cache_clear()


def test_migration_chain_is_linear_with_single_head() -> None:
    versions_dir = Path("alembic/versions")
    down_revisions: set[str] = set()
    revisions: set[str] = set()
    for path in versions_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("revision: str = "):
                revisions.add(line.split('"')[1])
            if line.startswith("down_revision: str | None = ") and '"' in line:
                down_revisions.add(line.split('"')[1])
    heads = revisions - down_revisions
    assert len(heads) == 1
    assert REVISION in revisions
    assert DOWN_REVISION in down_revisions
