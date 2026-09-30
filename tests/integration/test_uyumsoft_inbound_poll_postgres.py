"""PostgreSQL-only guarantees of the inbound poller: advisory lock + concurrent discovery."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.composition.imports import open_read_only_session
from app.models.workbench_review_item import WorkbenchReviewItem
from app.services.uyumsoft_inbound_poll import (
    POLL_ADVISORY_LOCK_KEY,
    POLL_STATUS_COMPLETED,
    POLL_STATUS_SKIPPED_LOCKED,
    PREVIEW_ALREADY_KNOWN,
    PREVIEW_NEW,
    InboundPollConfig,
    KnownInboundInvoiceChecker,
    PostgresAdvisoryPollLock,
    UyumsoftInboundPollPreview,
)
from tests.unit.test_uyumsoft_inbound_poll import FakeUyumsoft, Harness

pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_TEST_DATABASE_URL"),
    reason="POSTGRES_TEST_DATABASE_URL is required for PostgreSQL advisory-lock validation.",
)


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def engine() -> Engine:
    """The real migrated schema (``Base.metadata.create_all`` cannot build it on PostgreSQL)."""

    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    db_engine = create_engine(database_url)
    _reset_schema(db_engine)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
        capture_output=True,
    )
    try:
        yield db_engine
    finally:
        _reset_schema(db_engine)
        db_engine.dispose()


@pytest.fixture(autouse=True)
def _empty_tables(engine: Engine) -> None:
    with engine.begin() as connection:
        tables = connection.scalars(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'")
        ).all()
        if tables:
            connection.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))


def _reset_schema(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))


@pytest.fixture()
def harness(engine: Engine, tmp_path: Path) -> Harness:
    return Harness(engine=engine, storage_root=tmp_path / "documents")


def test_advisory_lock_is_exclusive_across_connections_and_released(engine: Engine) -> None:
    first = PostgresAdvisoryPollLock(engine)
    second = PostgresAdvisoryPollLock(engine)

    with first.hold() as first_acquired:
        with second.hold() as second_acquired:
            assert (first_acquired, second_acquired) == (True, False)
    with second.hold() as reacquired:
        assert reacquired is True


def test_crashed_holder_releases_the_lock(engine: Engine) -> None:
    crashed = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    assert crashed.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": POLL_ADVISORY_LOCK_KEY})
    with PostgresAdvisoryPollLock(engine).hold() as acquired:
        assert acquired is False

    crashed.invalidate()  # the process died: PostgreSQL drops the session and its lock
    crashed.close()

    with PostgresAdvisoryPollLock(engine).hold() as acquired:
        assert acquired is True


def test_two_workers_polling_at_once_only_one_runs(harness: Harness, engine: Engine) -> None:
    barrier = threading.Barrier(2)
    results: list[Any] = []

    def worker() -> None:
        client = FakeUyumsoft([1, 2], on_list=lambda: time.sleep(0.5))
        barrier.wait(timeout=10)
        results.append(harness.cycle(client, lock=PostgresAdvisoryPollLock(engine)).run())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(result.status for result in results) == sorted([POLL_STATUS_COMPLETED, POLL_STATUS_SKIPPED_LOCKED])
    assert harness.count(WorkbenchReviewItem) == 2


def test_concurrent_discovery_without_the_lock_still_creates_one_review(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt and braces: even if two cycles bypass the lock, unique constraints hold."""

    class NoLock:
        def hold(self) -> Any:
            return nullcontext(True)

    monkeypatch.setattr(KnownInboundInvoiceChecker, "is_known", lambda self, invoice: False)
    barrier = threading.Barrier(2)
    results: list[Any] = []

    def worker() -> None:
        client = FakeUyumsoft([1])
        barrier.wait(timeout=10)
        results.append(harness.cycle(client, lock=NoLock()).run())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 2
    assert harness.count(WorkbenchReviewItem) == 1


def test_preview_runs_in_a_postgres_read_only_transaction(harness: Harness, engine: Engine) -> None:
    harness.cycle(FakeUyumsoft([1])).run()
    preview = UyumsoftInboundPollPreview(
        read_session_scope=partial(open_read_only_session, engine),
        client=FakeUyumsoft([1, 2]),
        config=InboundPollConfig(),
    )

    result = preview.run()

    assert [item.status for item in result.items] == [PREVIEW_ALREADY_KNOWN, PREVIEW_NEW]
    with pytest.raises(Exception, match="read-only transaction"):  # the scope really is READ ONLY
        with open_read_only_session(engine) as session:
            session.execute(text("CREATE TEMP TABLE should_fail (id int)"))


def test_empty_cycle_leaves_no_sync_run_row_on_postgres(harness: Harness, engine: Engine) -> None:
    harness.cycle(FakeUyumsoft([1])).run()
    second = harness.cycle(FakeUyumsoft([1])).run()

    with engine.connect() as connection:
        runs = connection.scalar(text("SELECT count(*) FROM uyumsoft_sync_runs"))
    assert (second.audit_recorded, runs) == (False, 1)
