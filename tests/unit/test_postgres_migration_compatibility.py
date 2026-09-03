from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from alembic import command
from app.core.config import get_settings

pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_TEST_DATABASE_URL"),
    reason="POSTGRES_TEST_DATABASE_URL is required for PostgreSQL migration compatibility validation.",
)


def test_existing_postgresql_database_at_original_015_upgrades_to_head(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = postgres_database_url
    _alembic(monkeypatch, database_url, "upgrade", "202607170015")
    names_at_015 = _classification_names(database_url)

    assert names_at_015["constraints"] == {
        "ck_workbench_review_classification_evidence_company_id_positive",
        "ck_workbench_review_classification_evidence_review_version_posi",
        "ck_workbench_review_classification_evidence_schema_version_posi",
        "fk_workbench_review_classification_evidence_review_id",
        "uq_workbench_review_classification_evidence_company_review_vers",
    }
    assert names_at_015["indexes"] == {
        "ix_workbench_review_classification_evidence_company_review_vers",
        "ix_workbench_review_classification_evidence_rule_code",
        "ix_workbench_review_classification_evidence_status",
        "uq_workbench_review_classification_evidence_company_review_vers",
        "workbench_review_classification_evidence_pkey",
    }

    _alembic(monkeypatch, database_url, "upgrade", "head")

    assert _current_revision(database_url) == "202607170017"
    _assert_import_receipts_schema(database_url)


def test_fresh_postgresql_database_upgrades_directly_to_head(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = postgres_database_url

    _alembic(monkeypatch, database_url, "upgrade", "head")

    assert _current_revision(database_url) == "202607170017"
    _assert_import_receipts_schema(database_url)
    assert "workbench_review_classification_evidence" in inspect(create_engine(database_url)).get_table_names()


def test_postgresql_head_downgrades_to_015_and_reupgrades_without_duplicate_receipts(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = postgres_database_url
    _alembic(monkeypatch, database_url, "upgrade", "head")

    _alembic(monkeypatch, database_url, "downgrade", "202607170015")
    inspector = inspect(create_engine(database_url))
    assert _current_revision(database_url) == "202607170015"
    assert "import_receipts" not in inspector.get_table_names()
    assert "workbench_review_classification_evidence" in inspector.get_table_names()

    _alembic(monkeypatch, database_url, "upgrade", "head")

    assert _current_revision(database_url) == "202607170017"
    _assert_import_receipts_schema(database_url)


@pytest.fixture
def postgres_database_url() -> Iterator[str]:
    base_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    database_name = f"ict_pr99_{uuid4().hex}"
    _create_database(base_url, database_name)
    try:
        yield make_url(base_url).set(database=database_name).render_as_string(hide_password=False)
    finally:
        _drop_database(base_url, database_name)


def _create_database(base_url: str, database_name: str) -> None:
    admin_engine = _admin_engine(base_url)
    with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    admin_engine.dispose()


def _drop_database(base_url: str, database_name: str) -> None:
    admin_engine = _admin_engine(base_url)
    with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(
            text(
                "SELECT pg_terminate_backend(pid) "
                "FROM pg_stat_activity "
                "WHERE datname = :database_name AND pid <> pg_backend_pid()"
            ),
            {"database_name": database_name},
        )
        connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
    admin_engine.dispose()


def _admin_engine(base_url: str):
    return create_engine(make_url(base_url).set(database="postgres"))


def _alembic(monkeypatch: pytest.MonkeyPatch, database_url: str, operation: str, target: str) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    if operation == "upgrade":
        command.upgrade(config, target)
    else:
        command.downgrade(config, target)


def _classification_names(database_url: str) -> dict[str, set[str]]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            constraints = set(
                connection.execute(
                    text(
                        "SELECT conname "
                        "FROM pg_constraint "
                        "WHERE conrelid = 'workbench_review_classification_evidence'::regclass "
                        "AND contype IN ('c', 'f', 'u')"
                    )
                ).scalars()
            )
            indexes = set(
                connection.execute(
                    text(
                        "SELECT indexname "
                        "FROM pg_indexes "
                        "WHERE schemaname = 'public' "
                        "AND tablename = 'workbench_review_classification_evidence'"
                    )
                ).scalars()
            )
        return {"constraints": constraints, "indexes": indexes}
    finally:
        engine.dispose()


def _current_revision(database_url: str) -> str:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return str(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one())
    finally:
        engine.dispose()


def _assert_import_receipts_schema(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert inspector.get_table_names().count("import_receipts") == 1
        assert {
            "company_id",
            "idempotency_key",
            "invoice_id",
            "status",
            "vendor_bill_id",
            "review_id",
            "created_at",
        }.issubset({column["name"] for column in inspector.get_columns("import_receipts")})
        assert {
            "ix_import_receipts_company_status",
            "ix_import_receipts_idempotency_key",
        }.issubset({index["name"] for index in inspector.get_indexes("import_receipts")})
        assert {constraint["name"] for constraint in inspector.get_unique_constraints("import_receipts")} == {
            "uq_import_receipts_company_idempotency_key"
        }
    finally:
        engine.dispose()
