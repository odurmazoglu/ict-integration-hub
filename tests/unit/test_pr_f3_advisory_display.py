"""PR F / F3.1: advisory suggestion display (ADR-0014) -- gate, snapshot, publisher, sweep, wiring.

Production-shaped fixtures: the PR B/19F VİTEL review (``review:vitel``, line 1, seller code
``85710.1S1``, evidence partner 101) and the F2 ICT Bulut/ManageEngine catalog. The Odoo Studio
fake reads Many2one values back as ``[id, name]`` and applies Many2many ``[[6, 0, ids]]``
commands the way Odoo does, so idempotency is proven against Odoo's read shapes.
"""

from __future__ import annotations

import ast
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.advisory.product_suggestions import ProductSuggestion, ProductSuggestionResult, SuggestionTier
from app.advisory.suggestion_sets import (
    DISPLAY_SWEEP_INTERVAL,
    DISPLAY_VALIDITY,
    PROJECTION_CUTOFF,
    AdvisoryLine,
    DisplayBlock,
    DisplayedLine,
    SuggestionSetReason,
    SuggestionSetStatus,
    display_block,
    display_cleared,
    needs_display_refresh,
)
from app.application.workbench.dto import ReviewStatus
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workbench.product_line_projection import (
    ProductLineMatchState,
    WorkbenchProductLineProjection,
    product_line_key,
)
from app.application.workbench.product_line_suggestion_view import ProductLineSuggestionView
from app.application.workbench.projection_sync_contracts import (
    ProductLineSyncOutcome,
    ProjectionSyncOutcome,
    ProjectionSyncResult,
)
from app.composition import advisory as composition
from app.composition import advisory_display
from app.composition.advisory_display import (
    ADVISORY_UI_WARNING,
    AdvisoryDisplayRefresher,
    AdvisorySuggestionDisplayReader,
    render_summary,
)
from app.composition.imports import build_workbench_projection_synchronizer
from app.core.config import Settings
from app.core.runtime_checks import runtime_configuration_errors
from app.db.base import Base
from app.erp.odoo.workbench_product_line_publisher import (
    OdooWorkbenchProductLineFieldMapping,
    OdooWorkbenchProductLinePublisher,
)
from app.erp.odoo.workbench_projection_publisher import OdooWorkbenchProjectionPublisher
from app.models.workbench_product_line_suggestion import (
    WorkbenchProductLineSuggestionCandidate,
    WorkbenchProductLineSuggestionSet,
)
from app.persistence.product_line_suggestion_repository import (
    PRODUCT_NAME_MAX_LENGTH,
    SqlAlchemyProductLineSuggestionRepository,
)
from app.services.advisory_product_suggestions import AdvisoryTickStatus
from tests.unit.test_ops_ui_01a_workbench_projection_sync import _mapping as _parent_mapping
from tests.unit.test_p0_prod_19f_review_lifecycle_effective_state import (
    PRODUCT_NOT_FOUND_REASON,
    REVIEW_ID,
    VITEL_SKU,
    _seed,
)
from tests.unit.test_pr_b_product_line_projection import LINE, TwoModelStudio, _projection
from tests.unit.test_pr_f2_advisory_suggestion_worker import (
    COMPANY,
    ME,
    TFZP,
    WINDOWS_VM,
    _Catalog,
    _catalog,
    _Clock,
    _Lines,
    _service,
    _variant,
)

M2M = "x_studio_ipp_suggested_product_ids"
SUMMARY = "x_studio_ipp_suggestion_summary"
HINTS = replace(LINE, suggested_products=M2M, suggestion_summary=SUMMARY)
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
EVIDENCE_PARTNER = 101  # the partner the persisted VİTEL product matching ran under
VITEL_LINE_KEY = product_line_key(company_id=COMPANY, review_id=REVIEW_ID, line_number="1")
DFCCD66E = "review:dfccd66e-43e7-546b-b559-49323e79ab9b"
DFCCD66E_LINE_2 = product_line_key(company_id=COMPANY, review_id=DFCCD66E, line_number="2")
OPERATOR_INPUT_PREFIXES = ("x_studio_ipp_req_", "x_studio_ipp_new_")


# ------------------------------------------------------------------------------ builders


def _result(review_id: str, version: int, line: str, *candidates: tuple[Any, ...], notes=()) -> ProductSuggestionResult:
    return ProductSuggestionResult(
        review_id=review_id,
        line_number=line,
        review_version=version,
        catalog_fingerprint="sha256:x",
        suggestions=tuple(
            ProductSuggestion(
                rank=rank,
                product_id=product_id,
                tier=tier,
                score=Decimal(score),
                reasons=("r",),
                warnings=tuple(warnings),
                review_version=version,
                catalog_fingerprint="sha256:x",
            )
            for rank, product_id, tier, score, *warnings in candidates
        ),
        ambiguous=len({candidate[0] for candidate in candidates}) < len(candidates),
        notes=tuple(notes),
    )


def _advisory_line(review_id=REVIEW_ID, version=1, number="1", *, supplier=EVIDENCE_PARTNER, code=VITEL_SKU):
    return AdvisoryLine(
        company_id=COMPANY,
        review_id=review_id,
        review_version=version,
        line_number=number,
        line_key=product_line_key(company_id=COMPANY, review_id=review_id, line_number=number),
        line_sequence=1,
        supplier_partner_id=supplier,
        seller_item_code=code,
        description="ManageEngine Endpoint Central",
    )


def _insert(session, line=None, result=None, *, now=NOW, names=None) -> int:
    line = line or _advisory_line()
    result = result or _result(
        line.review_id, line.review_version, line.line_number, (1, 392, SuggestionTier.T1_EXACT_IDENTIFIER, "1.00")
    )
    set_id = SqlAlchemyProductLineSuggestionRepository(session).insert_current(
        line,
        result,
        catalog_fingerprint="sha256:c",
        supplierinfo_fingerprint="sha256:s",
        engine_version="f1.2",
        now=now,
        product_names=names if names is not None else {392: ME.name, 395: WINDOWS_VM.name},
    )
    session.commit()
    return set_id


def _stored(session, set_id):
    repository = SqlAlchemyProductLineSuggestionRepository(session)
    row = session.get(WorkbenchProductLineSuggestionSet, set_id)
    session.refresh(row)
    return repository._stored([row])[0]


def _mark(session, set_id, synced_at) -> None:
    repository = SqlAlchemyProductLineSuggestionRepository(session)
    repository.mark_display_synced([_stored(session, set_id)], synced_at=synced_at)


def _shown(**overrides) -> DisplayedLine:
    values = dict(
        review_pending=True,
        review_version=1,
        line_key=VITEL_LINE_KEY,
        line_number="1",
        product_not_found=True,
        supplier_partner_id=EVIDENCE_PARTNER,
        seller_item_code=VITEL_SKU,
    )
    values.update(overrides)
    return DisplayedLine(**values)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hub.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    yield engine, factory
    engine.dispose()


class M2MStudio(TwoModelStudio):
    """TwoModelStudio that applies Many2many commands and reads them back as id lists."""

    M2M_FIELDS = frozenset({M2M})

    def create(self, *, model: str, values: dict[str, Any]) -> int:
        return super().create(model=model, values=self._apply(values))

    def write(self, *, model: str, record_id: int, values: dict[str, Any]) -> None:
        self.raw_writes = [*getattr(self, "raw_writes", []), (record_id, dict(values))]
        super().write(model=model, record_id=record_id, values=self._apply(values))

    def _apply(self, values: dict[str, Any]) -> dict[str, Any]:
        applied = dict(values)
        for name in self.M2M_FIELDS & set(values):
            command = values[name]
            assert isinstance(command, list) and len(command) == 1 and command[0][:2] == [6, 0], command
            applied[name] = sorted(command[0][2])
        return applied


def _publisher(studio, mapping=HINTS) -> OdooWorkbenchProjectionPublisher:
    return OdooWorkbenchProjectionPublisher(
        adapter=studio,
        mapping=_parent_mapping(),
        product_line_publisher=OdooWorkbenchProductLinePublisher(adapter=studio, mapping=mapping),
    )


def _hint(key=DFCCD66E_LINE_2, ids=(392, 395), summary="1. ManageEngine — kesin tanımlayıcı eşleşmesi (skor 1.00)"):
    return ProductLineSuggestionView(line_key=key, product_ids=ids, summary=summary)


def _line_row(studio, number: str) -> dict[str, Any]:
    return next(row for row in studio.line_rows() if row[LINE.line_number] == number)


# ------------------------------------------------------------------------------ 1. pure display gate


def test_cutoff_is_one_sweep_interval_inside_the_unchanged_twenty_minute_validity() -> None:
    assert DISPLAY_VALIDITY == timedelta(minutes=20)
    assert DISPLAY_SWEEP_INTERVAL == timedelta(seconds=60)
    assert PROJECTION_CUTOFF == timedelta(minutes=19)


def test_gate_passes_only_when_every_condition_holds(db) -> None:
    _engine, factory = db
    with factory() as session:
        stored = _stored(session, _insert(session))
    assert display_block(stored, _shown(), now=NOW) is None
    assert display_block(stored, _shown(), now=NOW + PROJECTION_CUTOFF) is None


@pytest.mark.parametrize(
    ("shown", "block"),
    [
        (_shown(review_pending=False), DisplayBlock.REVIEW_NOT_PENDING),
        (_shown(review_version=2), DisplayBlock.VERSION_MISMATCH),
        (_shown(product_not_found=False), DisplayBlock.LINE_NOT_PRODUCT_NOT_FOUND),
        (_shown(line_key=f"{VITEL_LINE_KEY}x"), DisplayBlock.LINE_IDENTITY_MISMATCH),
        (_shown(line_number="2"), DisplayBlock.LINE_IDENTITY_MISMATCH),
        (_shown(supplier_partner_id=434), DisplayBlock.SUPPLIER_MISMATCH),
        (_shown(supplier_partner_id=None), DisplayBlock.SUPPLIER_MISMATCH),
        (_shown(seller_item_code="85710.1S3"), DisplayBlock.SELLER_CODE_MISMATCH),
        (_shown(seller_item_code=None), DisplayBlock.SELLER_CODE_MISMATCH),
    ],
)
def test_gate_rejects_each_mismatch(db, shown, block) -> None:
    _engine, factory = db
    with factory() as session:
        stored = _stored(session, _insert(session))
    assert display_block(stored, shown, now=NOW) is block


def test_gate_rejects_expired_retired_and_empty_sets(db) -> None:
    _engine, factory = db
    with factory() as session:
        stored = _stored(session, _insert(session))
        empty = _stored(session, _insert(session, _advisory_line(number="2"), _result(REVIEW_ID, 1, "2")))
    assert display_block(stored, _shown(), now=NOW + PROJECTION_CUTOFF + timedelta(seconds=1)) is (
        DisplayBlock.VALIDATION_EXPIRED
    )
    retired = replace(stored, status=SuggestionSetStatus.SUPERSEDED)
    assert display_block(retired, _shown(), now=NOW) is DisplayBlock.SET_NOT_CURRENT
    assert display_block(empty, _shown(line_key=empty.line_key, line_number="2"), now=NOW) is (
        DisplayBlock.NO_CANDIDATES
    )
    with pytest.raises(ValueError):
        display_block(stored, _shown(), now=NOW.replace(tzinfo=None))


def test_refresh_rule_skips_already_cleared_sets(db) -> None:
    _engine, factory = db
    with factory() as session:
        stored = _stored(session, _insert(session))
    expired = NOW + PROJECTION_CUTOFF + timedelta(seconds=1)
    assert needs_display_refresh(stored, now=NOW)  # never refreshed: publish once
    published = replace(stored, display_synced_at=NOW)
    assert not needs_display_refresh(published, now=NOW + timedelta(minutes=5))
    assert needs_display_refresh(published, now=expired)  # shown, now expired: clear
    cleared = replace(stored, display_synced_at=expired)
    assert display_cleared(cleared.validated_at, cleared.display_synced_at)
    assert not needs_display_refresh(cleared, now=expired + timedelta(hours=3))
    assert not needs_display_refresh(replace(published, status=SuggestionSetStatus.INVALIDATED), now=expired)


# ------------------------------------------------------------------------------ 2. snapshot + markers


def test_tick_snapshots_candidate_names_from_the_generating_catalog_read(db) -> None:
    _engine, factory = db
    windows_8 = _variant(396, "Microsoft Windows Server Standard (per VM) (8 Core)", template_id=164, category=44)
    windows_16 = _variant(397, "Microsoft Windows Server Standard (per VM) (16 Core)", template_id=164, category=44)
    line = replace(TFZP, seller_item_code="TFZX")  # no supplierinfo: name tier only
    with factory() as session:
        result = _service(session, _Lines([line]), _Catalog(_catalog(windows_8, windows_16)), _Clock(NOW)).run(
            company_id=COMPANY
        )
        assert result.generated == 1
        candidates = session.scalars(
            select(WorkbenchProductLineSuggestionCandidate).order_by(WorkbenchProductLineSuggestionCandidate.position)
        ).all()
    names = {candidate.product_id: candidate.product_name for candidate in candidates}
    catalog_names = {variant.product_id: variant.name for variant in (WINDOWS_VM, windows_8, windows_16)}
    assert names and all(names[product_id] == catalog_names[product_id] for product_id in names)
    assert len(set(names.values())) == len(names)  # variants keep their attribute-qualified names


def test_name_snapshot_is_truncated_and_missing_names_render_as_ids(db) -> None:
    _engine, factory = db
    with factory() as session:
        long_id = _insert(session, names={392: "X" * 900})
        legacy_id = _insert(session, _advisory_line(number="2"), names={})
        assert len(session.get(WorkbenchProductLineSuggestionCandidate, 1).product_name) == PRODUCT_NAME_MAX_LENGTH
        legacy = _stored(session, legacy_id)
        assert _stored(session, long_id).product_name(392) == "X" * PRODUCT_NAME_MAX_LENGTH
    assert legacy.product_names == ()
    assert render_summary(legacy).startswith("1. Ürün #392 — kesin tanımlayıcı eşleşmesi (skor 1.00)")


def test_retire_and_revalidate_reset_the_marker_only_when_the_display_state_changes(db) -> None:
    _engine, factory = db
    expired = NOW + PROJECTION_CUTOFF + timedelta(seconds=1)
    with factory() as session:
        repository = SqlAlchemyProductLineSuggestionRepository(session)
        shown_id, cleared_id = _insert(session), _insert(session, _advisory_line(number="2"))
        repository.mark_display_synced([_stored(session, shown_id)], synced_at=NOW)
        repository.mark_display_synced([_stored(session, cleared_id)], synced_at=expired)
        session.commit()
        # A shown set that is retired must be cleared once; an already-cleared one keeps its marker.
        repository.retire(
            shown_id, status=SuggestionSetStatus.SUPERSEDED, reason=SuggestionSetReason.INPUT_CHANGED, now=expired
        )
        session.commit()
        assert _stored(session, shown_id).display_synced_at is None
        assert _stored(session, cleared_id).display_synced_at == expired
        # Revalidating a cleared set makes it showable again: refresh once.
        assert repository.revalidate([cleared_id], now=expired) == 1
        session.commit()
        revalidated = _stored(session, cleared_id)
        assert (revalidated.validated_at, revalidated.display_synced_at) == (expired, None)


def test_marking_is_compare_and_set_and_never_moves_validity(db) -> None:
    _engine, factory = db
    with factory() as session:
        repository = SqlAlchemyProductLineSuggestionRepository(session)
        stale = _stored(session, _insert(session))
        repository.revalidate([stale.id], now=NOW + timedelta(minutes=15))
        session.commit()
        assert repository.mark_display_synced([stale], synced_at=NOW + timedelta(minutes=16)) == 0
        fresh = _stored(session, stale.id)
        assert repository.mark_display_synced([fresh], synced_at=NOW + timedelta(minutes=16)) == 1
        session.commit()
        assert _stored(session, stale.id).validated_at == NOW + timedelta(minutes=15)


# ------------------------------------------------------------------------------ 3. reader (gate at projection time)


def _vitel_projection(*, version=1, status=ReviewStatus.PENDING_REVIEW, state=ProductLineMatchState.PRODUCT_NOT_FOUND):
    line = WorkbenchProductLineProjection(
        line_key=VITEL_LINE_KEY,
        review_id=REVIEW_ID,
        company_id=COMPANY,
        review_version=version,
        line_number="1",
        line_sequence=1,
        supplier_partner_id=EVIDENCE_PARTNER,
        seller_item_code=VITEL_SKU,
        description="ManageEngine Endpoint Central",
        quantity=Decimal("1"),
        unit_code="C62",
        match_state=state,
        product_id=392 if state is ProductLineMatchState.MATCHED else None,
        matched_by="supplier_product_code" if state is ProductLineMatchState.MATCHED else None,
        message="x",
    )
    return _projection(review_id=REVIEW_ID, version=version, status=status, product_lines=(line,))


def test_reader_returns_a_view_only_for_the_current_gated_line(db) -> None:
    _engine, factory = db
    with factory() as session:
        _insert(session, names={392: ME.name})
        reader = AdvisorySuggestionDisplayReader(session, clock=lambda: NOW + timedelta(minutes=5))
        (view,) = reader(_vitel_projection())
    assert view.line_key == VITEL_LINE_KEY and view.product_ids == (392,)
    assert view.summary == f"1. {ME.name} — kesin tanımlayıcı eşleşmesi (skor 1.00)"


@pytest.mark.parametrize(
    "projection_kwargs",
    [
        {"version": 2},  # reclassified: the set belongs to version 1
        {"status": ReviewStatus.RESOLVED},
        {"status": ReviewStatus.DECISION_SUBMITTED},
        {"state": ProductLineMatchState.MATCHED},
        {"state": ProductLineMatchState.PRODUCT_AMBIGUOUS},
    ],
)
def test_reader_shows_nothing_for_other_versions_decided_reviews_or_other_states(db, projection_kwargs) -> None:
    _engine, factory = db
    with factory() as session:
        _insert(session)
        assert AdvisorySuggestionDisplayReader(session, clock=lambda: NOW)(_vitel_projection(**projection_kwargs)) == ()


def test_reader_never_reads_advisory_rows_for_a_decided_review(db, monkeypatch) -> None:
    _engine, factory = db
    monkeypatch.setattr(
        SqlAlchemyProductLineSuggestionRepository,
        "list_current_for_review",
        lambda *args, **kwargs: pytest.fail("decided reviews need no advisory read"),
    )
    with factory() as session:
        reader = AdvisorySuggestionDisplayReader(session, clock=lambda: NOW)
        assert reader(_vitel_projection(status=ReviewStatus.RESOLVED)) == ()


def test_reader_hides_a_set_past_the_projection_cutoff(db) -> None:
    _engine, factory = db
    with factory() as session:
        _insert(session)
        at_cutoff = AdvisorySuggestionDisplayReader(session, clock=lambda: NOW + PROJECTION_CUTOFF)
        past = AdvisorySuggestionDisplayReader(session, clock=lambda: NOW + PROJECTION_CUTOFF + timedelta(seconds=1))
        assert len(at_cutoff(_vitel_projection())) == 1
        assert past(_vitel_projection()) == ()


def test_summary_ranks_ties_and_lists_each_warning_once(db) -> None:
    _engine, factory = db
    tie = "Benzer skorlu birden fazla aday var; doğru ürünü operatör seçmelidir."
    result = _result(
        REVIEW_ID,
        1,
        "1",
        (1, 395, SuggestionTier.T3_NAME_SIMILARITY, "0.62", tie),
        (1, 396, SuggestionTier.T3_NAME_SIMILARITY, "0.60", tie),
        notes=("Not A",),
    )
    with factory() as session:
        stored = _stored(
            session, _insert(session, result=result, names={395: WINDOWS_VM.name, 396: f"{WINDOWS_VM.name} (8 Core)"})
        )
    assert render_summary(stored).splitlines() == [
        f"1. {WINDOWS_VM.name} — ürün adı benzerliği (skor 0.62)",
        f"1. {WINDOWS_VM.name} (8 Core) — ürün adı benzerliği (skor 0.60)",
        f"Uyarı: {tie}",
        "Uyarı: Not A",
    ]
    assert "Öneriler bilgilendirme amaçlıdır" in ADVISORY_UI_WARNING


# ------------------------------------------------------------------------------ 4. publisher (many2many + contract)


def test_hint_fields_are_created_updated_and_cleared_through_many2many_commands() -> None:
    studio = M2MStudio()
    publisher = _publisher(studio)

    publisher.sync_projection(_projection(product_line_suggestions=(_hint(),)), apply=True)
    created = [values for model, values in studio.creates if model == LINE.model]
    assert [values[M2M] for values in created] == [[], [392, 395]]
    assert (_line_row(studio, "2")[SUMMARY] or "").startswith("1. ManageEngine")
    assert _line_row(studio, "1")[SUMMARY] is None

    publisher.sync_projection(_projection(product_line_suggestions=(_hint(ids=(395,), summary="1. X"),)), apply=True)
    record_id, values = studio.raw_writes[-1]
    assert values == {M2M: [[6, 0, [395]]], SUMMARY: "1. X"}
    assert _line_row(studio, "2")[M2M] == [395]

    publisher.sync_projection(_projection(product_line_suggestions=()), apply=True)
    assert studio.raw_writes[-1] == (record_id, {M2M: [[6, 0, []]], SUMMARY: None})
    assert _line_row(studio, "2")[M2M] == []


def test_unchanged_hints_rewrite_nothing() -> None:
    studio = M2MStudio()
    publisher = _publisher(studio)
    projection = _projection(product_line_suggestions=(_hint(ids=(395, 392)),))
    publisher.sync_projection(projection, apply=True)
    writes = (len(studio.creates), len(studio.writes))

    result = publisher.sync_projection(projection, apply=True)

    assert [line.outcome for line in result.line_results] == [ProductLineSyncOutcome.NO_CHANGE] * 2
    assert (len(studio.creates), len(studio.writes)) == writes


def test_publisher_never_shows_a_hint_outside_the_display_contract() -> None:
    """Defense in depth: even a hint handed to the publisher is cleared on a matched line or decided review."""

    studio = M2MStudio()
    matched_key = product_line_key(company_id=COMPANY, review_id=DFCCD66E, line_number="1")
    hints = (_hint(), _hint(key=matched_key, ids=(393,)))
    _publisher(studio).sync_projection(_projection(product_line_suggestions=hints), apply=True)
    assert _line_row(studio, "1")[M2M] == []  # line 1 is MATCHED

    decided = M2MStudio()
    _publisher(decided).sync_projection(
        _projection(status=ReviewStatus.RESOLVED, product_line_suggestions=hints), apply=True
    )
    assert all(row[M2M] == [] and row[SUMMARY] is None for row in decided.line_rows())


def test_deactivated_rows_get_their_hints_cleared() -> None:
    studio = M2MStudio()
    publisher = _publisher(studio)
    publisher.sync_projection(_projection(product_line_suggestions=(_hint(),)), apply=True)
    (only_line_1,) = [line for line in _projection().product_lines if line.line_number == "1"]

    result = publisher.sync_projection(_projection(product_lines=(only_line_1,)), apply=True)

    assert ProductLineSyncOutcome.DEACTIVATED in [line.outcome for line in result.line_results]
    row = _line_row(studio, "2")
    assert (row[LINE.is_current], row[M2M], row[SUMMARY]) == (False, [], None)


def test_mapped_hint_fields_with_display_off_are_cleared() -> None:
    studio = M2MStudio()
    publisher = _publisher(studio)
    publisher.sync_projection(_projection(product_line_suggestions=(_hint(),)), apply=True)

    publisher.sync_projection(_projection(product_line_suggestions=None), apply=True)

    assert all(row[M2M] == [] and row[SUMMARY] is None for row in studio.line_rows())


def test_unmapped_hint_fields_keep_the_publisher_byte_equivalent() -> None:
    """Without the two Studio fields, reads and writes are exactly the pre-F3.1 ones -- hints or not."""

    assert LINE.read_fields() == [
        "id", "x_studio_ipp_workbench_id", "x_studio_ipp_line_key", "x_studio_ipp_is_current", "x_name",
        "x_studio_ipp_review_id", "x_studio_ipp_company_id", "x_studio_ipp_review_version",
        "x_studio_ipp_line_number", "x_studio_ipp_line_sequence", "x_studio_ipp_supplier_id",
        "x_studio_ipp_seller_code", "x_studio_ipp_description", "x_studio_ipp_quantity", "x_studio_ipp_uom_code",
        "x_studio_ipp_match_state", "x_studio_ipp_product_id", "x_studio_ipp_matched_by",
        "x_studio_ipp_line_message",
    ]  # fmt: skip
    assert not LINE.suggestions_mapped and LINE.m2m_fields() == frozenset()

    def run(**overrides) -> TwoModelStudio:
        studio = TwoModelStudio()
        publisher = _publisher(studio, mapping=LINE)
        publisher.sync_projection(_projection(**overrides), apply=True)
        (only_line_1,) = [line for line in _projection().product_lines if line.line_number == "1"]
        publisher.sync_projection(_projection(product_lines=(only_line_1,), **overrides), apply=True)
        return studio

    baseline, with_hints = run(), run(product_line_suggestions=(_hint(),))
    strip = {"x_studio_last_sync_at"}

    def payloads(studio):
        return (
            [(model, {k: v for k, v in values.items() if k not in strip}) for model, values in studio.creates],
            [(model, rid, {k: v for k, v in values.items() if k not in strip}) for model, rid, values in studio.writes],
            studio.calls,
        )

    assert payloads(with_hints) == payloads(baseline)
    child_writes = [values for model, _rid, values in baseline.writes if model == LINE.model]
    assert child_writes == [{LINE.is_current: False}]  # deactivation is still the single-field write


def test_partial_or_operator_input_hint_mappings_are_rejected(monkeypatch) -> None:
    with pytest.raises(WorkbenchContractError, match="mapped together"):
        replace(LINE, suggested_products=M2M)
    with pytest.raises(WorkbenchContractError, match="mapped together"):
        replace(LINE, suggestion_summary=SUMMARY)
    with pytest.raises(WorkbenchContractError, match="distinct"):
        replace(LINE, suggested_products=M2M, suggestion_summary=M2M)
    for name in ("x_studio_ipp_req_product_id", "x_studio_ipp_new_name"):
        with pytest.raises(WorkbenchContractError, match="operator input"):
            replace(LINE, suggested_products=name, suggestion_summary=SUMMARY)

    monkeypatch.setenv("ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTED_PRODUCTS_FIELD", M2M)
    with pytest.raises(WorkbenchContractError, match="mapped together"):
        OdooWorkbenchProductLineFieldMapping.from_environment()
    monkeypatch.setenv("ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTION_SUMMARY_FIELD", SUMMARY)
    assert OdooWorkbenchProductLineFieldMapping.from_environment() == HINTS
    monkeypatch.setenv("ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTION_SUMMARY_FIELD", " ")
    with pytest.raises(WorkbenchContractError, match="non-empty"):
        OdooWorkbenchProductLineFieldMapping.from_environment()


def test_no_req_or_new_field_is_ever_read_or_written() -> None:
    studio = M2MStudio()
    publisher = _publisher(studio)
    publisher.sync_projection(_projection(product_line_suggestions=(_hint(),)), apply=True)
    publisher.sync_projection(_projection(product_line_suggestions=()), apply=True)

    written = {name for _model, values in studio.creates for name in values}
    written |= {name for _model, _rid, values in studio.writes for name in values}
    assert written and not [name for name in written if name.startswith(OPERATOR_INPUT_PREFIXES)]
    assert not [name for name in HINTS.read_fields() if name.startswith(OPERATOR_INPUT_PREFIXES)]


# ------------------------------------------------------------------------------ 5. real composition (SQLite)


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


def _synchronizer(engine, studio, *, display: bool, mapping=HINTS):
    return build_workbench_projection_synchronizer(
        engine=engine,
        settings=_settings(display=display),
        projection_adapter=studio,
        mapping=_parent_mapping(),
        product_mapping_enabled=False,
        product_line_mapping=mapping,
    )


def test_end_to_end_current_set_is_projected_then_cleared_after_reclassification(hub) -> None:
    engine, factory = hub
    with factory() as session:
        set_id = _insert(session, now=datetime.now(UTC), names={392: ME.name})
    studio = M2MStudio()

    result = _synchronizer(engine, studio, display=True).sync(review_id=REVIEW_ID, company_id=COMPANY)

    assert result.outcome is ProjectionSyncOutcome.CREATED and not result.line_failed
    (row,) = studio.line_rows()
    assert row[M2M] == [392]
    assert row[SUMMARY] == f"1. {ME.name} — kesin tanımlayıcı eşleşmesi (skor 1.00)"

    # The set no longer matches the line identity -> the next sync of any flow clears it.
    with factory() as session:
        session.get(WorkbenchProductLineSuggestionSet, set_id).seller_item_code = "85710.1S3"
        session.commit()
    _synchronizer(engine, studio, display=True).sync(review_id=REVIEW_ID, company_id=COMPANY)
    (row,) = studio.line_rows()
    assert (row[M2M], row[SUMMARY]) == ([], None)


def test_end_to_end_display_off_reads_no_advisory_row(hub, monkeypatch) -> None:
    engine, factory = hub
    with factory() as session:
        _insert(session, now=datetime.now(UTC))
    monkeypatch.setattr(
        SqlAlchemyProductLineSuggestionRepository,
        "list_current_for_review",
        lambda *args, **kwargs: pytest.fail("display off must not read advisory rows"),
    )
    studio = M2MStudio()

    _synchronizer(engine, studio, display=False).sync(review_id=REVIEW_ID, company_id=COMPANY)

    (row,) = studio.line_rows()
    assert (row[M2M], row[SUMMARY]) == ([], None)


def test_end_to_end_hint_read_failure_fails_closed_and_keeps_the_lines(hub, monkeypatch) -> None:
    engine, _factory = hub

    def broken(*args, **kwargs):
        raise RuntimeError("advisory table unavailable")

    monkeypatch.setattr(SqlAlchemyProductLineSuggestionRepository, "list_current_for_review", broken)
    studio = M2MStudio()

    result = _synchronizer(engine, studio, display=True).sync(review_id=REVIEW_ID, company_id=COMPANY)

    assert result.outcome is ProjectionSyncOutcome.CREATED and not result.line_failed
    (row,) = studio.line_rows()
    assert row[LINE.match_state] == "product_not_found" and row[M2M] == []


def test_display_on_without_hint_fields_fails_at_composition(hub) -> None:
    engine, _factory = hub
    with pytest.raises(WorkbenchContractError, match="SUGGESTIONS_DISPLAY_ENABLED=true requires"):
        _synchronizer(engine, M2MStudio(), display=True, mapping=LINE)
    _synchronizer(engine, M2MStudio(), display=False, mapping=LINE)  # off: unchanged contract


# ------------------------------------------------------------------------------ 6. database-only sweep


class _Sync:
    def __init__(self, outcome=ProjectionSyncOutcome.UPDATED, line_error=None) -> None:
        self.calls: list[str] = []
        self.outcome = outcome
        self.line_error = line_error

    def __call__(self, *, review_id: str, company_id: int) -> ProjectionSyncResult:
        self.calls.append(review_id)
        return ProjectionSyncResult(review_id=review_id, outcome=self.outcome, applied=True, line_error=self.line_error)


class _Monotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _refresher(factory, sync, clock, monotonic=None) -> AdvisoryDisplayRefresher:
    return AdvisoryDisplayRefresher(
        session_factory=factory,
        company_id=COMPANY,
        synchronize=sync,
        clock=clock,
        monotonic=monotonic or _Monotonic(),
    )


def test_no_due_set_means_no_odoo_call(db) -> None:
    _engine, factory = db
    sync = _Sync()
    assert _refresher(factory, sync, _Clock(NOW)).sweep().due_sets == 0
    with factory() as session:
        set_id = _insert(session)
        _mark(session, set_id, NOW)
        session.commit()
    clock = _Clock(NOW + timedelta(minutes=10))
    assert _refresher(factory, sync, clock).sweep().due_sets == 0
    assert sync.calls == []


def test_new_set_is_published_once_and_an_expired_one_is_cleared_once(db) -> None:
    _engine, factory = db
    with factory() as session:
        set_id = _insert(session)
    sync, clock = _Sync(), _Clock(NOW + timedelta(seconds=30))
    refresher = _refresher(factory, sync, clock)

    assert refresher.sweep().refreshed == 1  # publish
    clock.advance(minutes=5)
    assert refresher.sweep().due_sets == 0
    clock.now = NOW + PROJECTION_CUTOFF + timedelta(seconds=30)
    assert refresher.sweep().refreshed == 1  # clear
    for _ in range(3):
        clock.advance(minutes=1)
        assert refresher.sweep().due_sets == 0  # never again: no duplicate churn
    assert sync.calls == [REVIEW_ID, REVIEW_ID]
    with factory() as session:
        stored = _stored(session, set_id)
    assert stored.validated_at == NOW  # clearing never moved validity
    assert display_cleared(stored.validated_at, stored.display_synced_at)


@pytest.mark.parametrize(
    "sync",
    [
        _Sync(outcome=ProjectionSyncOutcome.ERROR),
        _Sync(outcome=ProjectionSyncOutcome.SKIPPED_STALE),
        _Sync(line_error="Odoo Workbench product line publish failed."),
    ],
)
def test_failed_clearing_stays_retryable_and_never_extends_validity(db, sync) -> None:
    _engine, factory = db
    with factory() as session:
        set_id = _insert(session)
        _mark(session, set_id, NOW)
        session.commit()
    clock, monotonic = _Clock(NOW + PROJECTION_CUTOFF + timedelta(seconds=5)), _Monotonic()
    refresher = _refresher(factory, sync, clock, monotonic)

    assert refresher.sweep().failed == 1
    assert refresher.sweep().backing_off == 1  # inside the 60 s retry delay: no Odoo call
    monotonic.value += 60
    assert refresher.sweep().failed == 1  # retried
    monotonic.value += 60
    assert refresher.sweep().backing_off == 1  # backoff doubled to 120 s
    assert len(sync.calls) == 2

    with factory() as session:
        stored = _stored(session, set_id)
    assert (stored.validated_at, stored.display_synced_at) == (NOW, NOW)
    assert needs_display_refresh(stored, now=clock.now)

    sync.outcome, sync.line_error = ProjectionSyncOutcome.NO_CHANGE, None
    monotonic.value += 60
    assert refresher.sweep().refreshed == 1
    with factory() as session:
        assert not needs_display_refresh(_stored(session, set_id), now=clock.now)


def test_retired_sets_are_cleared_once_and_the_sweep_is_bounded(db) -> None:
    _engine, factory = db
    with factory() as session:
        ids = [_insert(session, _advisory_line(review_id=f"review:{index:02d}")) for index in range(30)]
        repository = SqlAlchemyProductLineSuggestionRepository(session)
        repository.mark_display_synced([_stored(session, set_id) for set_id in ids], synced_at=NOW)
        for set_id in ids:
            repository.retire(
                set_id, status=SuggestionSetStatus.INVALIDATED, reason=SuggestionSetReason.LINE_RESOLVED, now=NOW
            )
        session.commit()
    sync = _Sync()
    refresher = _refresher(factory, sync, _Clock(NOW + timedelta(minutes=1)))

    first = refresher.sweep()
    second = refresher.sweep()
    third = refresher.sweep()

    assert (first.due_reviews, first.refreshed, first.deferred) == (30, 25, 5)
    assert (second.refreshed, third.due_sets) == (5, 0)
    assert sorted(sync.calls) == sorted(f"review:{index:02d}" for index in range(30))


def test_catalog_outage_ages_sets_out_and_the_sweep_clears_them(db) -> None:
    _engine, factory = db
    clock = _Clock(NOW)
    catalog = _Catalog(_catalog())
    line = replace(TFZP, review_id=REVIEW_ID, review_version=1, line_number="1")
    with factory() as session:
        _service(session, _Lines([line]), catalog, clock).run(company_id=COMPANY)
    sync = _Sync()
    refresher = _refresher(factory, sync, clock)
    refresher.sweep()  # published

    catalog.error = RuntimeError("Odoo down")
    for minutes in (15, 19):
        clock.now = NOW + timedelta(minutes=minutes)
        with factory() as session:
            assert _service(session, _Lines([line]), catalog, clock).run(company_id=COMPANY).status is (
                AdvisoryTickStatus.CATALOG_FAILED
            )
        refresher.sweep()
    assert len(sync.calls) == 1  # still within the cutoff: nothing to clear
    clock.now = NOW + PROJECTION_CUTOFF + timedelta(seconds=10)
    refresher.sweep()
    assert len(sync.calls) == 2  # cleared before the 20-minute validity ends


def test_post_tick_refresh_runs_after_the_tick_inside_the_lock(db, monkeypatch) -> None:
    _engine, factory = db
    order: list[str] = []

    class _Service:
        def run(self, *, company_id):
            order.append("tick")
            return "result"

    class _Refresher:
        def sweep(self):
            order.append("refresh")
            raise RuntimeError("a refresh failure never loses the tick result")

    monkeypatch.setattr(composition, "advisory_service", lambda *args, **kwargs: _Service())
    tick = composition.AdvisoryTick(
        session_factory=factory,
        lock=composition.advisory_lock(_engine),
        company_id=COMPANY,
        catalog_reader_factory=lambda: None,
        display_refresher=_Refresher(),
    )
    assert tick.run() == "result"
    assert order == ["tick", "refresh"]


# ------------------------------------------------------------------------------ 7. flags, worker, boundary


def _worker_settings(**overrides) -> Settings:
    values = dict(
        odoo_workbench_operator_requests_enabled=True,
        odoo_workbench_operator_requests_company_id=1,
        odoo_workbench_projection_publish_enabled=True,
        odoo_workbench_product_line_projection_enabled=True,
        odoo_workbench_product_line_suggestions_enabled=True,
        uyumsoft_inbound_poll_enabled=False,
    )
    values.update(overrides)
    return Settings(**values)


def test_display_flag_defaults_off_and_requires_the_worker_and_child_lines() -> None:
    assert Settings().odoo_workbench_product_line_suggestions_display_enabled is False
    errors = runtime_configuration_errors(Settings(odoo_workbench_product_line_suggestions_display_enabled=True))
    assert any("DISPLAY_ENABLED=true requires ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTIONS_ENABLED" in e for e in errors)
    assert any("DISPLAY_ENABLED=true requires ODOO_WORKBENCH_PRODUCT_LINE_PROJECTION_ENABLED" in e for e in errors)
    assert not [
        error
        for error in runtime_configuration_errors(
            _worker_settings(odoo_workbench_product_line_suggestions_display_enabled=True)
        )
        if "SUGGESTIONS" in error
    ]


@pytest.mark.parametrize("display", [False, True])
def test_worker_registers_the_sixty_second_sweep_only_with_the_display_flag(monkeypatch, display) -> None:
    from app.workers import uyumsoft_inbound_poller

    assert timedelta(seconds=uyumsoft_inbound_poller.DISPLAY_SWEEP_INTERVAL_SECONDS) == DISPLAY_SWEEP_INTERVAL
    registered: list[tuple[str, float]] = []
    real_scheduler = uyumsoft_inbound_poller.PeriodicTaskScheduler

    def recording_scheduler(*, tasks, stop_event):
        registered.extend((task.name, task.interval_seconds) for task in tasks)
        return real_scheduler(tasks=tasks, stop_event=stop_event)

    monkeypatch.setattr(uyumsoft_inbound_poller, "PeriodicTaskScheduler", recording_scheduler)
    ran: list[str] = []
    uyumsoft_inbound_poller.main(
        ["--once"],
        settings=_worker_settings(odoo_workbench_product_line_suggestions_display_enabled=display),
        stop_event=threading.Event(),
        cycle_builder=lambda _settings: pytest.fail("polling disabled"),
        operator_request_tick_builder=lambda _settings: lambda: ran.append("requests"),
        advisory_tick_builder=lambda _settings: lambda: ran.append("advisory"),
        advisory_display_sweep_builder=(
            (lambda _settings: lambda: ran.append("sweep"))
            if display
            else (lambda _settings: pytest.fail("no sweep without the display flag"))
        ),
    )
    expected = [("workbench_operator_requests", 60), ("advisory_product_suggestions", 900)]
    if display:
        expected.append(("advisory_suggestion_display_sweep", 60))
    assert registered == expected
    assert ran == ["requests", "advisory", *(["sweep"] if display else [])]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_display_adapter_has_no_composition_import_cycle() -> None:
    """``imports`` composes the reader and ``advisory`` imports ``imports``: the adapter must import neither."""

    display = _imports(Path(advisory_display.__file__))
    assert not [name for name in display if name.startswith("app.composition")]
    assert not [name for name in display if name.startswith(("app.services", "app.erp", "app.cli", "app.workers"))]
    imports_module = _imports(Path(advisory_display.__file__).with_name("imports.py"))
    assert "app.composition.advisory" not in imports_module
    assert "app.composition.advisory_display" in imports_module


def test_the_workbench_package_never_imports_the_advisory_package() -> None:
    root = Path(advisory_display.__file__).resolve().parents[1] / "application" / "workbench"
    offenders = [
        path.name for path in root.rglob("*.py") if any(name.startswith("app.advisory") for name in _imports(path))
    ]
    assert offenders == []
