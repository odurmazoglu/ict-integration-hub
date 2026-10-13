"""PR F / F2: advisory suggestion migration, partial unique index and single-flight on real PostgreSQL."""

from __future__ import annotations

import importlib.util
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.advisory.product_suggestions import CatalogSnapshot, SuggestionPolicy, suggest_products
from app.advisory.suggestion_sets import AdvisoryLine
from app.composition.advisory import AdvisoryTick, advisory_lock
from app.db.base import Base
from app.models.workbench_review_item import WorkbenchReviewItem
from app.persistence.product_line_suggestion_repository import SqlAlchemyProductLineSuggestionRepository
from app.services.advisory_product_suggestions import HubLineSnapshot

pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_TEST_DATABASE_URL"),
    reason="POSTGRES_TEST_DATABASE_URL is required for focused PostgreSQL advisory validation.",
)

REVIEW = "review:pr-f2-advisory"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
SETS = "workbench_product_line_suggestion_sets"
CANDIDATES = "workbench_product_line_suggestion_candidates"
MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "202607170041_product_line_suggestions.py"
LINE = AdvisoryLine(
    company_id=1, review_id=REVIEW, review_version=2, line_number="1", line_key="ipp-pl:v1:1:x:1", line_sequence=1,
    supplier_partner_id=24, seller_item_code="TFZP", description="Contoso Backup Agent",
)  # fmt: skip


def _migration():
    spec = importlib.util.spec_from_file_location("migration_0041", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run(engine: Engine, step: str) -> None:
    module = _migration()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            getattr(module, step)()


@pytest.fixture()
def engine():
    bound = create_engine(os.environ["POSTGRES_TEST_DATABASE_URL"])
    with bound.begin() as connection:
        for table in (CANDIDATES, SETS):
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        connection.execute(text(f"DROP TABLE IF EXISTS {WorkbenchReviewItem.__tablename__} CASCADE"))
    Base.metadata.create_all(bound, tables=[WorkbenchReviewItem.__table__])
    with sessionmaker(bind=bound)() as session:
        session.add(
            WorkbenchReviewItem(
                review_id=REVIEW, company_id=1, invoice_id="ETTN-F2", invoice_number="ICF-F2", invoice_date=None,
                supplier_name="ICT BULUT", supplier_tax_number="4650459971", currency="TRY", total_amount=None,
                status="pending_review", workflow="manual_review", review_reasons=[], warnings=[], version=2,
                idempotency_key="uyumsoft:1:ETTN-F2",
            )
        )  # fmt: skip
        session.commit()
    _run(bound, "upgrade")
    yield bound
    with bound.begin() as connection:
        for table in (CANDIDATES, SETS, WorkbenchReviewItem.__tablename__):
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    bound.dispose()


def _insert(engine: Engine) -> None:
    with sessionmaker(bind=engine)() as session:
        result = suggest_products(LINE.suggestion_line(), CatalogSnapshot(variants=()), policy=SuggestionPolicy())
        SqlAlchemyProductLineSuggestionRepository(session).insert_current(
            LINE,
            result,
            catalog_fingerprint="sha256:c",
            supplierinfo_fingerprint="sha256:s",
            engine_version="f1.1",
            now=NOW,
        )
        session.commit()


def test_migration_creates_the_partial_unique_index_and_downgrades_cleanly(engine) -> None:
    inspector = inspect(engine)
    assert {SETS, CANDIDATES}.issubset(inspector.get_table_names())
    with engine.connect() as connection:
        definition = connection.scalar(text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ux_wpls_current_line'"))
    assert "UNIQUE" in definition and "WHERE" in definition and "current" in definition

    _run(engine, "downgrade")
    assert not {SETS, CANDIDATES} & set(inspect(engine).get_table_names())
    _run(engine, "upgrade")
    assert {SETS, CANDIDATES}.issubset(inspect(engine).get_table_names())


def test_two_concurrent_writers_can_never_create_two_current_sets(engine) -> None:
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def writer() -> None:
        barrier.wait()
        try:
            _insert(engine)
            outcomes.append("inserted")
        except IntegrityError:
            outcomes.append("rejected")

    threads = [threading.Thread(target=writer) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["inserted", "rejected"]
    with engine.connect() as connection:
        assert connection.scalar(text(f"SELECT count(*) FROM {SETS} WHERE status = 'current'")) == 1


class _BlockingCatalog:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def read(self, *, company_id: int) -> CatalogSnapshot:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(10)
        return CatalogSnapshot(variants=())


def test_concurrent_advisory_ticks_are_single_flight(engine, monkeypatch) -> None:
    from app.composition import advisory as composition

    class _Lines:
        def __init__(self, session) -> None:
            pass

        def snapshot(self, *, company_id: int) -> HubLineSnapshot:
            return HubLineSnapshot(lines=(LINE,), pending_versions={REVIEW: 2})

    class _Priors:
        def __init__(self, session) -> None:
            pass

        def prior_mappings(self, *, company_id: int):
            return ()

    monkeypatch.setattr(composition, "HubAdvisoryLineSource", _Lines)
    monkeypatch.setattr(composition, "HubPriorMappingSource", _Priors)
    catalog = _BlockingCatalog()

    def tick() -> AdvisoryTick:
        return AdvisoryTick(
            session_factory=sessionmaker(bind=engine),
            lock=advisory_lock(engine),
            company_id=1,
            catalog_reader_factory=lambda: catalog,
        )

    first_result: list[object] = []
    first = threading.Thread(target=lambda: first_result.append(tick().run()))
    first.start()
    assert catalog.entered.wait(10)
    second = tick().run()  # the first tick holds the PostgreSQL advisory lock
    catalog.release.set()
    first.join()

    assert second is None
    assert catalog.calls == 1
    assert first_result and first_result[0] is not None
    with engine.connect() as connection:
        assert connection.scalar(text(f"SELECT count(*) FROM {SETS} WHERE status = 'current'")) == 1
