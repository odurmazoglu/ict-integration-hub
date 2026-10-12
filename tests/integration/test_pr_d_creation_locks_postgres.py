"""PR D: creation-key and lock behaviour on real PostgreSQL (unique constraints, transactions)."""

from __future__ import annotations

import os
import threading
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.application.workbench.exceptions import ProductRemediationDataIntegrityError, ProductRemediationRaceError
from app.application.workbench.product_line_creation import (
    NewProductIntent,
    NewProductKind,
    ProductLineCreation,
    ProductLineCreationStatus,
)
from app.db.base import Base
from app.models.workbench_product_line_creation import WorkbenchProductLineCreation
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_product_identity_claim import WorkbenchReviewProductIdentityClaim
from app.persistence import SqlAlchemyProductLineCreationRepository, SqlAlchemyReviewProductIdentityClaimRepository

pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_TEST_DATABASE_URL"),
    reason="POSTGRES_TEST_DATABASE_URL is required for focused PostgreSQL concurrency validation.",
)

REVIEW = "review:pr-d-locks"
TABLES = [
    WorkbenchReviewItem.__table__,
    WorkbenchProductLineCreation.__table__,
    WorkbenchReviewProductIdentityClaim.__table__,
]


def _drop(engine: Engine) -> None:
    with engine.begin() as connection:
        for table in reversed(TABLES):
            connection.execute(text(f"DROP TABLE IF EXISTS {table.name} CASCADE"))


@pytest.fixture()
def factory():
    engine = create_engine(os.environ["POSTGRES_TEST_DATABASE_URL"])
    _drop(engine)
    Base.metadata.create_all(engine, tables=TABLES)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as session:
        session.add(
            WorkbenchReviewItem(
                review_id=REVIEW,
                company_id=1,
                invoice_id="ETTN-PR-D",
                invoice_number="ICF-1",
                supplier_tax_number="4650459971",
                supplier_name="ICT BULUT",
                invoice_date=date(2026, 9, 30),
                currency="TRY",
                total_amount=Decimal("100.00"),
                workflow="manual_review",
                status="pending_review",
                review_reasons=[{"code": "product_not_found", "message": "x", "line_number": "1"}],
                warnings=[],
                version=2,
                idempotency_key="uyumsoft:1:ETTN-PR-D",
            )
        )
        session.commit()
    try:
        yield session_factory
    finally:
        _drop(engine)
        engine.dispose()


def _creation(code: str = "TFZP", **overrides) -> ProductLineCreation:
    values = dict(
        review_id=REVIEW,
        company_id=1,
        review_version=2,
        line_number="1",
        status=ProductLineCreationStatus.CREATE_ATTEMPTED,
        supplier_partner_id=24,
        seller_item_code=code,
        intent=NewProductIntent(product_name="Windows Server", kind=NewProductKind.SERVICE, categ_id=60, uom_id=1),
        approved_by="operator",
        create_attempted_at=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
    )
    values.update(overrides)
    return ProductLineCreation(**values)


def _race(factory, work) -> list[str]:
    barrier = threading.Barrier(2)
    results: list[str] = []

    def run(index: int) -> None:
        with factory() as session:
            barrier.wait(timeout=10)
            try:
                work(session, index)
                session.commit()
                results.append("won")
            except ProductRemediationRaceError:
                session.rollback()
                results.append("lost")

    threads = [threading.Thread(target=run, args=(index,)) for index in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    return sorted(results)


def test_two_lines_racing_for_one_supplier_code_have_exactly_one_winner(factory) -> None:
    def claim(session, index: int) -> None:
        owner = _creation().identity_claim()
        SqlAlchemyReviewProductIdentityClaimRepository(session).claim(replace(owner, owner_line_number=str(index + 1)))

    assert _race(factory, claim) == ["lost", "won"]
    with factory() as session:
        assert session.query(WorkbenchReviewProductIdentityClaim).count() == 1


def test_two_requests_racing_for_one_line_have_exactly_one_winner_and_the_loser_keeps_nothing(factory) -> None:
    def attempt(session, index: int) -> None:
        # mirrors the use case: claim (a per-thread code, so only the line insert can collide), then insert
        code = f"CODE-{index}"
        SqlAlchemyReviewProductIdentityClaimRepository(session).claim(_creation(code).identity_claim())
        SqlAlchemyProductLineCreationRepository(session).insert_attempt(_creation(code))

    assert _race(factory, attempt) == ["lost", "won"]
    with factory() as session:
        assert session.query(WorkbenchProductLineCreation).count() == 1
        # the loser's claim was rolled back together with its failed insert
        assert session.query(WorkbenchReviewProductIdentityClaim).count() == 1


def test_discard_and_release_only_touch_an_unresolved_attempt_and_its_own_claim(factory) -> None:
    with factory() as session:
        claims = SqlAlchemyReviewProductIdentityClaimRepository(session)
        repository = SqlAlchemyProductLineCreationRepository(session)
        attempt = repository.insert_attempt(_creation())
        claims.claim(attempt.identity_claim())
        session.commit()

        claims.release(replace(attempt.identity_claim(), owner_review_version=1))  # another owner: no-op
        repository.discard_attempt(attempt)
        claims.release(attempt.identity_claim())
        session.commit()
        assert session.query(WorkbenchProductLineCreation).count() == 0
        assert session.query(WorkbenchReviewProductIdentityClaim).count() == 0

        created = repository.advance(
            repository.insert_attempt(_creation()),
            expected_status=ProductLineCreationStatus.CREATE_ATTEMPTED,
            new_status=ProductLineCreationStatus.PRODUCT_CREATED,
            product_template_id=900,
            product_id=9900,
        )
        session.commit()
        with pytest.raises(ProductRemediationDataIntegrityError, match="refusing to discard"):
            repository.discard_attempt(created)


def test_migrated_schema_accepts_pr_d_claims_and_still_locks_across_both_owner_kinds(monkeypatch) -> None:
    """Regression: the claim owner FK used to point at the REST reservation table only, so every
    PR D claim failed on PostgreSQL. Runs the real migrations, then the real repositories."""

    from alembic.config import Config
    from sqlalchemy.engine import make_url

    from alembic import command
    from app.core.config import get_settings
    from tests.unit.test_postgres_migration_compatibility import _create_database, _drop_database

    base_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    name = f"ict_pr_d_{os.getpid()}"
    _create_database(base_url, name)
    url = make_url(base_url).set(database=name).render_as_string(hide_password=False)
    try:
        monkeypatch.setenv("DATABASE_URL", url)
        get_settings.cache_clear()
        command.upgrade(Config("alembic.ini"), "head")
        engine = create_engine(url)
        session_factory = sessionmaker(bind=engine)
        with session_factory() as session:
            session.add(
                WorkbenchReviewItem(
                    review_id=REVIEW,
                    company_id=1,
                    invoice_id="ETTN-PR-D",
                    invoice_number="ICF-1",
                    supplier_tax_number="4650459971",
                    supplier_name="ICT BULUT",
                    invoice_date=date(2026, 9, 30),
                    currency="TRY",
                    total_amount=Decimal("100.00"),
                    workflow="manual_review",
                    status="pending_review",
                    review_reasons=[],
                    warnings=[],
                    version=2,
                    idempotency_key="uyumsoft:1:ETTN-PR-D",
                )
            )
            claims = SqlAlchemyReviewProductIdentityClaimRepository(session)
            attempt = _creation()
            claims.claim(attempt.identity_claim())
            SqlAlchemyProductLineCreationRepository(session).insert_attempt(attempt)
            session.commit()

            # a REST-flow claim for the same identity still loses against the PR D owner
            rest_claim = replace(attempt.identity_claim(), owner_line_number="9", owner_kind="remediation_reservation")
            with pytest.raises(ProductRemediationRaceError):
                claims.claim(rest_claim)
            session.rollback()
        engine.dispose()
    finally:
        get_settings.cache_clear()
        _drop_database(base_url, name)
