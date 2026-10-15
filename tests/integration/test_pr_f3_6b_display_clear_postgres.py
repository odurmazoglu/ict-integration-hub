"""PR F / F3.6b on PostgreSQL: compare-and-set marker reset and the shared advisory lock.

``clear-display`` holds the same ``pg_try_advisory_lock`` key as the generation tick and the
display sweep, so it never interleaves with them across processes; a held lock means nothing is
written. The marker reset touches only current sets of the company that carry a marker.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.advisory.product_suggestions import ENGINE_VERSION
from app.advisory.suggestion_sets import SuggestionSetReason, SuggestionSetStatus
from app.composition.advisory import (
    ADVISORY_SUGGESTIONS_LOCK_KEY,
    AdvisoryDisplayCleaner,
    DisplayClearOutcome,
    advisory_lock,
)
from app.erp.odoo.workbench_suggestion_field_cleaner import OdooWorkbenchSuggestionFieldCleaner
from app.models.workbench_product_line_suggestion import WorkbenchProductLineSuggestionSet
from app.persistence.product_line_suggestion_repository import SqlAlchemyProductLineSuggestionRepository
from app.services.uyumsoft_inbound_poll import PostgresAdvisoryPollLock
from tests.integration.test_pr_f3_advisory_display_postgres import LINE, NOW, RESULT, pytestmark  # noqa: F401
from tests.integration.test_pr_f3_advisory_display_postgres import engine as engine  # noqa: F401 - fixture
from tests.unit.test_pr_b_product_line_projection import LINE as CHILD
from tests.unit.test_pr_f3_6b_display_clear import M2M, SUMMARY, ClearStudio
from tests.unit.test_pr_f3_advisory_display import HINTS


def _insert(session, line, *, marked: bool) -> int:
    repository = SqlAlchemyProductLineSuggestionRepository(session)
    set_id = repository.insert_current(
        line, RESULT, catalog_fingerprint="sha256:c", supplierinfo_fingerprint="sha256:s",
        engine_version=ENGINE_VERSION, now=NOW,
    )  # fmt: skip
    if marked:
        (stored,) = [item for item in repository.list_current(company_id=line.company_id) if item.id == set_id]
        repository.mark_display_synced([stored], synced_at=NOW + timedelta(seconds=5))
    session.commit()
    return set_id


def _markers(factory) -> dict[int, object]:
    with factory() as session:
        rows = session.execute(
            select(WorkbenchProductLineSuggestionSet.id, WorkbenchProductLineSuggestionSet.display_synced_at)
        ).all()
    return {row.id: row.display_synced_at for row in rows}


def test_marker_reset_is_compare_and_set_on_current_sets_of_the_company_only(engine) -> None:
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as session:
        marked = _insert(session, LINE, marked=True)
        unmarked = _insert(session, replace(LINE, line_number="2", line_key="ipp-pl:v1:1:x:2"), marked=False)
        retired = _insert(session, replace(LINE, line_number="3", line_key="ipp-pl:v1:1:x:3"), marked=True)
        other_company = _insert(session, replace(LINE, company_id=2, line_key="ipp-pl:v1:2:x:1"), marked=True)
        SqlAlchemyProductLineSuggestionRepository(session).retire(
            retired, status=SuggestionSetStatus.SUPERSEDED, reason=SuggestionSetReason.CATALOG_CHANGED, now=NOW
        )
        session.commit()
    retired_marker = _markers(factory)[retired]

    with factory() as session:
        repository = SqlAlchemyProductLineSuggestionRepository(session)
        assert repository.count_display_marked_current(company_id=1) == 1
        assert repository.reset_display_markers(company_id=1) == 1
        session.commit()
        assert repository.reset_display_markers(company_id=1) == 0  # idempotent
        assert repository.count_display_marked_current(company_id=1) == 0

    markers = _markers(factory)
    assert markers[marked] is None and markers[unmarked] is None
    assert markers[retired] == retired_marker  # history untouched
    assert markers[other_company] is not None  # other company untouched


def test_a_held_advisory_lock_blocks_the_clear_and_releasing_it_lets_it_finish(engine) -> None:
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as session:
        set_id = _insert(session, LINE, marked=True)
    studio = ClearStudio()
    record_id = studio.add_child(**{CHILD.company_id: 1, CHILD.is_current: True, M2M: [395], SUMMARY: "x"})

    def cleaner() -> AdvisoryDisplayCleaner:
        return AdvisoryDisplayCleaner(
            cleaner=OdooWorkbenchSuggestionFieldCleaner(adapter=studio, mapping=HINTS),
            session_factory=factory,
            lock=advisory_lock(engine),
            company_id=1,
            sleep=lambda _seconds: None,
            lock_attempts=2,
        )

    with PostgresAdvisoryPollLock(engine, key=ADVISORY_SUGGESTIONS_LOCK_KEY).hold() as held:  # e.g. a running tick
        assert held
        busy = cleaner().apply()
    assert busy.outcome is DisplayClearOutcome.LOCK_BUSY
    assert studio.writes == [] and _markers(factory)[set_id] is not None

    done = cleaner().apply()

    assert done.complete and done.cleared == (record_id,) and done.markers_reset == 1
    assert studio.rows[CHILD.model][record_id][M2M] == [] and _markers(factory)[set_id] is None


def test_the_tick_and_sweep_lock_is_busy_while_clear_display_holds_it(engine) -> None:
    """The reverse direction: a tick or sweep started during the clear finds the lock taken and skips."""

    with advisory_lock(engine).hold() as acquired:
        assert acquired is True
        with PostgresAdvisoryPollLock(engine, key=ADVISORY_SUGGESTIONS_LOCK_KEY).hold() as tick:
            assert tick is False
