"""PR F / F3.6b: ``clear-display`` -- explicit, verified display rollback (ADR-0014).

The Odoo fake is the F3 ``M2MStudio`` (Odoo read shapes, Many2many ``[[6, 0, ids]]`` commands)
extended with ``!=`` domains, pagination and injectable timeouts. The Hub is the real SQLite
schema seeded with the production-shaped VİTEL review used by F3.1.
"""

from __future__ import annotations

import ast
import io
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.application.workbench.exceptions import WorkbenchContractError
from app.cli import advisory_product_suggestions as cli
from app.composition import advisory as composition
from app.composition.advisory import CLEAR_SETTLE, DisplayClearOutcome
from app.core.config import Settings
from app.db.base import Base
from app.erp.exceptions import ErpRepositoryError, ErpRepositoryTimeoutError
from app.erp.odoo.workbench_suggestion_field_cleaner import OdooWorkbenchSuggestionFieldCleaner
from app.models.workbench_product_line_suggestion import WorkbenchProductLineSuggestionSet
from app.persistence.product_line_suggestion_repository import SqlAlchemyProductLineSuggestionRepository
from tests.unit.test_p0_prod_19f_review_lifecycle_effective_state import (
    PRODUCT_NOT_FOUND_REASON,
    REVIEW_ID,
    VITEL_SKU,
    _seed,
)
from tests.unit.test_pr_b_product_line_projection import LINE, LINE_MODEL
from tests.unit.test_pr_f2_advisory_suggestion_worker import COMPANY, ME, _Clock
from tests.unit.test_pr_f3_advisory_display import (
    HINTS,
    M2M,
    SUMMARY,
    M2MStudio,
    _insert,
    _line_row,
    _refresher,
)

OPERATOR_INPUTS = {"x_studio_ipp_req_product_id": 77, "x_studio_ipp_new_name": "Operatör girdisi"}
SETTLE_SECONDS = CLEAR_SETTLE.total_seconds()


class ClearStudio(M2MStudio):
    """``M2MStudio`` plus ``!=`` domains, offset paging and injectable Odoo timeouts."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fail_writes: dict[int, int] = {}
        self.fail_reads = 0
        self.raw_writes: list[tuple[int, dict[str, Any]]] = []

    def search_read(self, *, model: str, domain: list[Any], fields: list[str], limit: int, offset: int = 0):
        if model != LINE_MODEL or not any(op == "!=" for _name, op, _value in domain):
            return super().search_read(model=model, domain=domain, fields=fields, limit=limit, offset=offset)
        self.calls.append(("search_read", model))
        if self.fail_reads:
            self.fail_reads -= 1
            raise ErpRepositoryTimeoutError("Odoo request timed out.")
        rows = [
            (record_id, row)
            for record_id, row in sorted(self.rows[model].items())
            if all(_odoo_match(row.get(name), op, value) for name, op, value in domain)
        ]
        shaped = tuple(self._read_shape(model, record_id, row, fields) for record_id, row in rows)
        return shaped[offset : offset + limit]

    def write(self, *, model: str, record_id: int, values: dict[str, Any]) -> None:
        if self.fail_writes.get(record_id):
            self.fail_writes[record_id] -= 1
            raise ErpRepositoryTimeoutError("Odoo request timed out.")
        super().write(model=model, record_id=record_id, values=values)

    def add_child(self, **values: Any) -> int:
        record_id = self._next_id[LINE_MODEL]
        self._next_id[LINE_MODEL] += 1
        self.rows[LINE_MODEL][record_id] = dict(values)
        return record_id

    def child_writes(self) -> list[tuple[int, dict[str, Any]]]:
        return [(record_id, values) for model, record_id, values in self.writes if model == LINE_MODEL]


def _odoo_match(stored: Any, op: str, value: Any) -> bool:
    if op == "!=" and value is False:
        return bool(stored)  # Odoo: a Many2many / text field "is set"
    return stored == value


class _Sleeps:
    def __init__(self, on_settle=None) -> None:
        self.calls: list[float] = []
        self.on_settle = on_settle

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if seconds == SETTLE_SECONDS and self.on_settle is not None:
            self.on_settle()


class _BusyLock:
    @contextmanager
    def hold(self):
        yield False


@pytest.fixture()
def hub(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hub.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as session:
        _seed(session, product_id=None, seller_item_code=VITEL_SKU, reasons=(PRODUCT_NOT_FOUND_REASON,))
        session.commit()
    yield engine, factory
    engine.dispose()


def _settings(*, display: bool) -> Settings:
    return Settings(
        odoo_workbench_product_line_projection_enabled=True,
        odoo_workbench_product_line_suggestions_display_enabled=display,
    )


def _synchronizer(engine, studio, *, display: bool):
    from app.composition.imports import build_workbench_projection_synchronizer
    from tests.unit.test_ops_ui_01a_workbench_projection_sync import _mapping as _parent_mapping

    return build_workbench_projection_synchronizer(
        engine=engine,
        settings=_settings(display=display),
        projection_adapter=studio,
        mapping=_parent_mapping(),
        product_mapping_enabled=False,
        product_line_mapping=HINTS,
    )


def _cleaner(engine, studio, sleeps=None, **options):
    return composition.build_advisory_display_cleaner(
        settings=_settings(display=False),
        engine=engine,
        company_id=COMPANY,
        adapter=studio,
        mapping=HINTS,
        sleep=sleeps or _Sleeps(),
        **options,
    )


def _published(hub, studio, now=None):
    """Display ON: the F3.1 sweep publishes the VİTEL T1 suggestion (product 392) once."""

    engine, factory = hub
    now = now or datetime.now(UTC)
    with factory() as session:
        set_id = _insert(session, now=now, names={392: ME.name})
    synchronizer = _synchronizer(engine, studio, display=True)
    refresher = _refresher(factory, synchronizer.sync, _Clock(now + timedelta(seconds=5)))
    assert refresher.sweep().refreshed == 1
    row = _line_row(studio, "1")
    assert row[M2M] == [392] and row[SUMMARY]
    return set_id, refresher


def _record_id(studio, number: str = "1") -> int:
    return next(rid for rid, row in studio.rows[LINE_MODEL].items() if row.get(LINE.line_number) == number)


def _marker(factory, set_id):
    with factory() as session:
        return session.get(WorkbenchProductLineSuggestionSet, set_id).display_synced_at


def _set_count(factory) -> int:
    with factory() as session:
        return session.scalar(select(func.count()).select_from(WorkbenchProductLineSuggestionSet))


def _without_suggestions(row: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in row.items() if name not in {M2M, SUMMARY}}


# ------------------------------------------------------------------------------ critical lifecycle


def test_display_on_publish_off_clear_verify_on_republish_lifecycle(hub) -> None:
    engine, factory = hub
    studio = ClearStudio()
    set_id, _refresher_on = _published(hub, studio)
    record_id = _record_id(studio)
    studio.rows[LINE_MODEL][record_id].update(OPERATOR_INPUTS)
    published = (list(_line_row(studio, "1")[M2M]), _line_row(studio, "1")[SUMMARY])
    untouched = _without_suggestions(studio.rows[LINE_MODEL][record_id])
    sets_before, creates_before = _set_count(factory), len(studio.creates)
    assert _marker(factory, set_id) is not None

    # Display OFF -> clear-display
    writes_before = len(studio.child_writes())
    report = _cleaner(engine, studio).apply()

    assert report.outcome is DisplayClearOutcome.CLEARED and report.complete
    assert report.cleared == (record_id,) and report.remaining == () and report.markers_reset == 1
    assert (_line_row(studio, "1")[M2M], _line_row(studio, "1")[SUMMARY]) == ([], False)
    assert studio.child_writes()[writes_before:] == [(record_id, {M2M: [], SUMMARY: False})]
    assert studio.raw_writes[-1] == (record_id, {M2M: [[6, 0, []]], SUMMARY: False})
    assert _without_suggestions(studio.rows[LINE_MODEL][record_id]) == untouched  # product, req_*, new_* intact
    assert _marker(factory, set_id) is None

    # idempotent: nothing left, nothing written
    writes_after_clear = len(studio.child_writes())
    again = _cleaner(engine, studio).apply()
    assert again.complete and again.discovered == () and again.markers_reset == 0
    assert len(studio.child_writes()) == writes_after_clear

    # Display ON again -> the re-armed sweep republishes exactly once
    now = datetime.now(UTC)
    refresher = _refresher(factory, _synchronizer(engine, studio, display=True).sync, _Clock(now))
    assert refresher.sweep().refreshed == 1
    row = _line_row(studio, "1")
    assert (list(row[M2M]), row[SUMMARY]) == published  # same candidate ids, same line, same text
    republish = studio.child_writes()[writes_after_clear:]
    assert [record for record, _values in republish] == [record_id]
    assert set(republish[0][1]) == {M2M, SUMMARY}
    assert refresher.sweep().due_sets == 0  # no duplicate write
    assert len(studio.child_writes()) == writes_after_clear + 1
    assert _set_count(factory) == sets_before  # no new suggestion set
    assert len(studio.creates) == creates_before  # no product (or any other record) created
    assert _without_suggestions(studio.rows[LINE_MODEL][record_id]) == untouched
    assert row[LINE.product] in (None, False)  # never mapped


def test_a_worker_still_running_with_display_on_fails_the_clear(hub) -> None:
    engine, factory = hub
    studio = ClearStudio()
    set_id, refresher_on = _published(hub, studio)

    report = _cleaner(engine, studio, _Sleeps(on_settle=refresher_on.sweep)).apply()

    assert report.outcome is DisplayClearOutcome.REPOPULATED and not report.complete
    assert report.markers_after_settle == 1 and [row.record_id for row in report.remaining] == [_record_id(studio)]
    assert _line_row(studio, "1")[M2M] == [392]
    assert _marker(factory, set_id) is not None


def test_display_off_activity_during_the_settle_window_does_not_fail_the_clear(hub) -> None:
    engine, factory = hub
    studio = ClearStudio()
    set_id, _refresher_on = _published(hub, studio)
    synchronizer_off = _synchronizer(engine, studio, display=False)

    def display_off_tick_and_operator_sync() -> None:
        with factory() as session:  # the F2 tick revalidates (display OFF: no refresh, no marker)
            SqlAlchemyProductLineSuggestionRepository(session).revalidate([set_id], now=datetime.now(UTC))
            session.commit()
        synchronizer_off.sync(review_id=REVIEW_ID, company_id=COMPANY)  # an operator flow (mapped, display off)

    report = _cleaner(engine, studio, _Sleeps(on_settle=display_off_tick_and_operator_sync)).apply()

    assert report.complete and report.markers_after_settle == 0
    row = _line_row(studio, "1")
    assert row[M2M] == [] and not row[SUMMARY]


# ------------------------------------------------------------------------------ discovery and payload


def test_non_current_rows_are_found_and_cleared_and_other_companies_are_not_touched(hub) -> None:
    engine, _factory = hub
    studio = ClearStudio()
    _published(hub, studio)
    stale = studio.add_child(
        **{LINE.company_id: COMPANY, LINE.is_current: False, LINE.line_key: "ipp-pl:v1:1:old:1",
           LINE.review_id: "review:old", M2M: [], SUMMARY: "Güvenli ürün önerisi yok."}
    )  # fmt: skip
    other = studio.add_child(**{LINE.company_id: 2, LINE.is_current: True, M2M: [395], SUMMARY: "başka şirket"})

    report = _cleaner(engine, studio).apply()

    assert report.complete
    assert sorted(report.cleared) == sorted([_record_id(studio), stale])
    assert studio.rows[LINE_MODEL][stale][SUMMARY] is False
    assert studio.rows[LINE_MODEL][other][M2M] == [395]  # company 2 untouched


def test_every_clear_write_holds_exactly_the_two_suggestion_fields(hub) -> None:
    engine, _factory = hub
    studio = ClearStudio()
    _published(hub, studio)
    studio.add_child(**{LINE.company_id: COMPANY, LINE.is_current: False, M2M: [392], SUMMARY: False})
    before = len(studio.raw_writes)

    _cleaner(engine, studio).apply()

    for _record, values in studio.raw_writes[before:]:
        assert values == {M2M: [[6, 0, []]], SUMMARY: False}
        assert not any(name.startswith(("x_studio_ipp_req_", "x_studio_ipp_new_")) for name in values)
        assert LINE.product not in values and LINE.match_state not in values


@pytest.mark.parametrize("values", [{M2M: [[6, 0, []]]}, {M2M: [[6, 0, []]], SUMMARY: False, LINE.product: False}])
def test_the_payload_guard_rejects_anything_but_the_two_fields(values) -> None:
    cleaner = OdooWorkbenchSuggestionFieldCleaner(adapter=ClearStudio(), mapping=HINTS)
    with pytest.raises(WorkbenchContractError):
        cleaner._check_payload(values)


def test_unmapped_suggestion_fields_cannot_build_a_cleaner() -> None:
    with pytest.raises(WorkbenchContractError, match="SUGGESTED_PRODUCTS_FIELD"):
        OdooWorkbenchSuggestionFieldCleaner(adapter=ClearStudio(), mapping=LINE)


def test_discovery_pages_and_fails_closed_above_the_bound() -> None:
    studio = ClearStudio()
    for _ in range(5):
        studio.add_child(**{LINE.company_id: COMPANY, LINE.is_current: True, M2M: [392], SUMMARY: "x"})

    rows = OdooWorkbenchSuggestionFieldCleaner(adapter=studio, mapping=HINTS, page_size=2).populated_rows(
        company_id=COMPANY
    )
    assert len(rows) == 5 and [row.summary_digest is not None for row in rows] == [True] * 5

    bounded = OdooWorkbenchSuggestionFieldCleaner(adapter=studio, mapping=HINTS, page_size=2, max_rows=3)
    with pytest.raises(ErpRepositoryError, match="Too many"):
        bounded.populated_rows(company_id=COMPANY)


# ------------------------------------------------------------------------------ retries and fail-closed


def test_a_timed_out_write_is_retried_with_backoff_and_then_succeeds(hub) -> None:
    engine, _factory = hub
    studio = ClearStudio()
    _published(hub, studio)
    studio.fail_writes[_record_id(studio)] = 2
    sleeps = _Sleeps()

    report = _cleaner(engine, studio, sleeps).apply()

    assert report.complete
    assert sleeps.calls == [5.0, 10.0, SETTLE_SECONDS]


def test_a_write_that_keeps_failing_leaves_the_run_incomplete_and_the_markers_untouched(hub) -> None:
    engine, factory = hub
    studio = ClearStudio()
    set_id, _refresher_on = _published(hub, studio)
    record_id = _record_id(studio)
    studio.fail_writes[record_id] = 99
    sleeps = _Sleeps()

    report = _cleaner(engine, studio, sleeps).apply()

    assert report.outcome is DisplayClearOutcome.INCOMPLETE and not report.complete
    assert report.failed == (record_id,) and [row.record_id for row in report.remaining] == [record_id]
    assert report.markers_reset == 0 and _marker(factory, set_id) is not None
    assert SETTLE_SECONDS not in sleeps.calls  # never settles (or reports success) on a partial clear
    assert len(sleeps.calls) == 2  # bounded: 3 attempts, 2 waits


def test_a_read_that_keeps_timing_out_writes_nothing(hub) -> None:
    engine, factory = hub
    studio = ClearStudio()
    set_id, _refresher_on = _published(hub, studio)
    studio.fail_reads = 3
    writes = len(studio.writes)

    report = _cleaner(engine, studio).apply()

    assert report.outcome is DisplayClearOutcome.READ_FAILED and "timed out" in report.error
    assert len(studio.writes) == writes and _marker(factory, set_id) is not None


def test_a_single_read_timeout_is_retried(hub) -> None:
    engine, _factory = hub
    studio = ClearStudio()
    _published(hub, studio)
    studio.fail_reads = 1

    assert _cleaner(engine, studio).apply().complete


def test_a_busy_advisory_lock_writes_nothing() -> None:
    from app.composition.advisory import AdvisoryDisplayCleaner

    studio = ClearStudio()
    studio.add_child(**{LINE.company_id: COMPANY, LINE.is_current: True, M2M: [392], SUMMARY: "x"})
    sleeps = _Sleeps()
    cleaner = AdvisoryDisplayCleaner(
        cleaner=OdooWorkbenchSuggestionFieldCleaner(adapter=studio, mapping=HINTS),
        session_factory=lambda: pytest.fail("no Hub access without the lock"),
        lock=_BusyLock(),
        company_id=COMPANY,
        sleep=sleeps,
        lock_attempts=4,
    )

    report = cleaner.apply()

    assert report.outcome is DisplayClearOutcome.LOCK_BUSY and studio.writes == []
    assert sleeps.calls == [3.0, 3.0, 3.0]


def test_only_the_display_refresher_ever_sets_a_display_marker() -> None:
    """The settle check relies on it: a re-marked set proves a display-enabled refresh ran."""

    app_root = Path(composition.__file__).resolve().parents[1]
    callers = sorted(
        path.relative_to(app_root.parent).as_posix()
        for path in app_root.rglob("*.py")
        if "mark_display_synced" in {
            node.attr for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Attribute)
        }
    )  # fmt: skip
    assert callers == ["app/composition/advisory_display.py"]


# ------------------------------------------------------------------------------ CLI


def _cli(argv, hub, studio, *, display=False, sleeps=None, mapping=HINTS):
    engine, _factory = hub
    out = io.StringIO()
    code = cli.main(
        ["clear-display", "--company", str(COMPANY), *argv],
        engine=engine,
        settings=_settings(display=display),
        out=out,
        display_cleaner_options={"adapter": studio, "mapping": mapping, "sleep": sleeps or _Sleeps()},
    )
    return code, out.getvalue()


def test_cli_refuses_while_display_is_on(hub) -> None:
    studio = ClearStudio()
    code, output = _cli(["--apply", "--confirm-apply"], hub, studio, display=True)

    assert code == cli.EXIT_REFUSED and "REFUSED" in output and studio.calls == []


def test_cli_refuses_without_both_suggestion_fields(hub) -> None:
    studio = ClearStudio()
    code, output = _cli(["--apply", "--confirm-apply"], hub, studio, mapping=LINE)

    assert code == cli.EXIT_REFUSED and "SUGGESTED_PRODUCTS_FIELD" in output and studio.calls == []


def test_cli_refuses_a_partial_mapping_from_the_environment(hub, monkeypatch) -> None:
    engine, _factory = hub
    monkeypatch.setenv("ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTED_PRODUCTS_FIELD", M2M)
    monkeypatch.delenv("ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTION_SUMMARY_FIELD", raising=False)
    out = io.StringIO()

    code = cli.main(
        ["clear-display", "--company", "1"],
        engine=engine,
        settings=_settings(display=False),
        out=out,
        display_cleaner_options={"adapter": ClearStudio()},
    )

    assert code == cli.EXIT_REFUSED and "together" in out.getvalue()


def test_cli_apply_requires_confirmation_and_a_long_enough_settle(hub) -> None:
    studio = ClearStudio()
    assert _cli(["--apply"], hub, studio)[0] == cli.EXIT_USAGE
    with pytest.raises(SystemExit):
        _cli(["--settle-seconds", "30"], hub, studio)
    assert studio.calls == []


def test_cli_dry_run_reports_rows_without_invoice_text_and_writes_nothing(hub) -> None:
    _engine, factory = hub
    studio = ClearStudio()
    set_id, _refresher_on = _published(hub, studio)
    summary = _line_row(studio, "1")[SUMMARY]
    writes = len(studio.writes)

    code, output = _cli([], hub, studio)

    assert code == cli.EXIT_OK and "DRY-RUN" in output
    assert f"row={_record_id(studio)}" in output and "products=[392]" in output and "sha256:" in output
    assert summary not in output and ME.name not in output  # no summary / product text in the audit
    assert len(studio.writes) == writes and _marker(factory, set_id) is not None


def test_cli_apply_clears_and_exits_zero_only_when_verified(hub) -> None:
    studio = ClearStudio()
    _published(hub, studio)

    code, output = _cli(["--apply", "--confirm-apply"], hub, studio)

    assert code == cli.EXIT_OK
    assert "CLEARED" in output and "outcome=cleared" in output and "may now be removed" in output


def test_cli_apply_exits_non_zero_while_rows_remain(hub) -> None:
    studio = ClearStudio()
    _published(hub, studio)
    studio.fail_writes[_record_id(studio)] = 99

    code, output = _cli(["--apply", "--confirm-apply"], hub, studio)

    assert code == cli.EXIT_FAILED
    assert "REMAINING" in output and "NOT COMPLETE" in output and "Keep both field mappings" in output


def test_after_a_clear_a_display_off_reconcile_plans_no_change(hub) -> None:
    """The clear writes ``False``; the mapped-but-off publisher wants ``None`` -- equal for Odoo, no churn."""

    engine, _factory = hub
    studio = ClearStudio()
    _published(hub, studio)
    assert _cleaner(engine, studio).apply().complete

    result = _synchronizer(engine, studio, display=False).plan(review_id=REVIEW_ID, company_id=COMPANY)

    assert [line.outcome.value for line in result.line_results] == ["no_change"]
    assert result.changes == ()
