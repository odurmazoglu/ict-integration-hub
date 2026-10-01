"""PostgreSQL validation of the append-only source-identity correction.

* the CLI's dry-run runs inside a PostgreSQL READ ONLY transaction, so any write
  attempt on that path would fail loudly -- this proves the dry-run is write-free;
* two concurrent ``--apply`` runs for the same review yield exactly one correction
  and one version advance.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.application.workbench.exceptions import ReviewVersionConflictError
from app.application.workbench.source_identity_correction import SourceIdentityCorrectionOutcome
from app.composition.imports import open_read_only_session
from app.core.config import get_settings
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_source_invoice_correction import WorkbenchReviewSourceInvoiceCorrection
from app.services.document_storage import LocalDocumentStorage
from tests.unit.test_review_source_identity_correction import AY_STYLE, Env, _Partners, _seed_historical_review

pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_TEST_DATABASE_URL"),
    reason="POSTGRES_TEST_DATABASE_URL is required for PostgreSQL source-correction validation.",
)


@pytest.fixture()
def engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """A private, throw-away database migrated to head exactly like production."""

    base_url = make_url(os.environ["POSTGRES_TEST_DATABASE_URL"])
    name = f"source_correction_{uuid4().hex[:12]}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    database_url = base_url.set(database=name)
    monkeypatch.setenv("DATABASE_URL", database_url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()
    db_engine = create_engine(database_url)
    try:
        yield db_engine
    finally:
        db_engine.dispose()
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def _env(engine: Engine, session: Session, root: Path, partners: _Partners) -> Env:
    return Env(engine, session, LocalDocumentStorage(root), partners, root)


async def test_dry_run_succeeds_inside_a_read_only_transaction(engine: Engine, tmp_path: Path) -> None:
    partners = _Partners()
    with sessionmaker(bind=engine)() as session:
        review_id = await _seed_historical_review(_env(engine, session, tmp_path, partners), AY_STYLE)

    with open_read_only_session(engine) as read_only:
        report = await _env(engine, read_only, tmp_path, partners).correct(review_id, apply=False)

    assert report.outcome is SourceIdentityCorrectionOutcome.WOULD_APPLY
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(WorkbenchReviewSourceInvoiceCorrection)) == 0
        assert connection.scalar(select(WorkbenchReviewItem.version)) == 1


async def test_concurrent_applies_produce_exactly_one_correction(engine: Engine, tmp_path: Path) -> None:
    partners = _Partners()
    with sessionmaker(bind=engine)() as session:
        review_id = await _seed_historical_review(_env(engine, session, tmp_path, partners), AY_STYLE)

    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def run() -> None:
        with sessionmaker(bind=engine)() as session:
            env = _env(engine, session, tmp_path, partners)
            barrier.wait()
            try:
                outcomes.append(asyncio.run(env.correct(review_id, apply=True)).outcome)
            except ReviewVersionConflictError as exc:
                outcomes.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count(SourceIdentityCorrectionOutcome.APPLIED) == 1
    assert all(
        outcome is SourceIdentityCorrectionOutcome.APPLIED
        or outcome is SourceIdentityCorrectionOutcome.ALREADY_APPLIED
        or isinstance(outcome, ReviewVersionConflictError)
        for outcome in outcomes
    )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(WorkbenchReviewSourceInvoiceCorrection)) == 1
        version, vat = connection.execute(
            select(WorkbenchReviewItem.version, WorkbenchReviewItem.supplier_tax_number)
        ).one()
    assert (version, vat) == (2, AY_STYLE.vkn)
