"""PR F / F3.1: display migration and refresh-marker semantics on real PostgreSQL."""

from __future__ import annotations

import importlib.util
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.advisory.product_suggestions import ENGINE_VERSION, ProductSuggestion, ProductSuggestionResult, SuggestionTier
from app.advisory.suggestion_sets import (
    PROJECTION_CUTOFF,
    AdvisoryLine,
    SuggestionSetReason,
    SuggestionSetStatus,
)
from app.db.base import Base
from app.models.workbench_review_item import WorkbenchReviewItem
from app.persistence.product_line_suggestion_repository import SqlAlchemyProductLineSuggestionRepository

pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_TEST_DATABASE_URL"),
    reason="POSTGRES_TEST_DATABASE_URL is required for focused PostgreSQL advisory validation.",
)

REVIEW = "review:pr-f3-display"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
SETS = "workbench_product_line_suggestion_sets"
CANDIDATES = "workbench_product_line_suggestion_candidates"
VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
LINE = AdvisoryLine(
    company_id=1, review_id=REVIEW, review_version=2, line_number="1", line_key="ipp-pl:v1:1:x:1", line_sequence=1,
    supplier_partner_id=24, seller_item_code="TFZP", description="Microsoft Windows Server Standard",
)  # fmt: skip
RESULT = ProductSuggestionResult(
    review_id=REVIEW,
    line_number="1",
    review_version=2,
    catalog_fingerprint="sha256:x",
    suggestions=(
        ProductSuggestion(
            rank=1, product_id=395, tier=SuggestionTier.T3_NAME_SIMILARITY, score=0.62, reasons=("r",), warnings=(),
            review_version=2, catalog_fingerprint="sha256:x",
        ),
    ),
)  # fmt: skip


def _migration(name: str):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / name)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run(engine: Engine, name: str, step: str) -> None:
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            getattr(_migration(name), step)()


F2 = "202607170041_product_line_suggestions.py"
F3 = "202607170042_product_line_suggestion_display.py"


@pytest.fixture()
def engine():
    bound = create_engine(os.environ["POSTGRES_TEST_DATABASE_URL"])
    with bound.begin() as connection:
        for table in (CANDIDATES, SETS, WorkbenchReviewItem.__tablename__):
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    Base.metadata.create_all(bound, tables=[WorkbenchReviewItem.__table__])
    with sessionmaker(bind=bound)() as session:
        session.add(
            WorkbenchReviewItem(
                review_id=REVIEW, company_id=1, invoice_id="ETTN-F3", invoice_number="ICF-F3", invoice_date=None,
                supplier_name="ICT BULUT", supplier_tax_number="4650459971", currency="TRY", total_amount=None,
                status="pending_review", workflow="manual_review", review_reasons=[], warnings=[], version=2,
                idempotency_key="uyumsoft:1:ETTN-F3",
            )
        )  # fmt: skip
        session.commit()
    _run(bound, F2, "upgrade")
    _run(bound, F3, "upgrade")
    yield bound
    with bound.begin() as connection:
        for table in (CANDIDATES, SETS, WorkbenchReviewItem.__tablename__):
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))


def _columns(engine: Engine, table: str) -> dict[str, dict]:
    return {column["name"]: column for column in inspect(engine).get_columns(table)}


def test_upgrade_adds_two_nullable_columns_and_downgrade_removes_only_them(engine) -> None:
    assert _columns(engine, CANDIDATES)["product_name"]["nullable"] is True
    synced = _columns(engine, SETS)["display_synced_at"]
    assert synced["nullable"] is True and synced["type"].timezone is True
    before = {table: set(_columns(engine, table)) for table in (SETS, CANDIDATES)}

    _run(engine, F3, "downgrade")

    assert set(_columns(engine, SETS)) == before[SETS] - {"display_synced_at"}
    assert set(_columns(engine, CANDIDATES)) == before[CANDIDATES] - {"product_name"}
    _run(engine, F3, "upgrade")


def test_snapshot_marker_and_due_query_on_postgres(engine) -> None:
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as session:
        repository = SqlAlchemyProductLineSuggestionRepository(session)
        set_id = repository.insert_current(
            LINE, RESULT, catalog_fingerprint="sha256:c", supplierinfo_fingerprint="sha256:s",
            engine_version=ENGINE_VERSION, now=NOW, product_names={395: "Microsoft Windows Server Standard (per VM)"},
        )  # fmt: skip
        session.commit()
        (due,) = repository.display_refresh_due(company_id=1, now=NOW)
        assert due.product_name(395) == "Microsoft Windows Server Standard (per VM)"
        # timestamptz compare-and-set: the read value matches exactly on PostgreSQL.
        assert repository.mark_display_synced([due], synced_at=NOW) == 1
        session.commit()
        assert repository.display_refresh_due(company_id=1, now=NOW + timedelta(minutes=10)) == ()
        expired = NOW + PROJECTION_CUTOFF + timedelta(seconds=1)
        (clear,) = repository.display_refresh_due(company_id=1, now=expired)
        assert repository.mark_display_synced([clear], synced_at=expired) == 1
        session.commit()
        assert repository.display_refresh_due(company_id=1, now=expired + timedelta(hours=1)) == ()
        # An already-cleared set keeps its marker on retirement: no second refresh.
        assert repository.retire(
            set_id, status=SuggestionSetStatus.INVALIDATED, reason=SuggestionSetReason.LINE_RESOLVED, now=expired
        )
        session.commit()
        assert repository.display_refresh_due(company_id=1, now=expired + timedelta(hours=1)) == ()
        validated = session.execute(text(f"SELECT validated_at FROM {SETS} WHERE id = :id"), {"id": set_id}).scalar()
        assert validated == NOW
