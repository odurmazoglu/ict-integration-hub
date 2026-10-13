"""PR F / F2: advisory suggestion persistence, worker lifecycle, CLI and flag safety (ADR-0014).

Production-shaped fixtures (read-only survey 2026-10-13): ICT Bulut (partner 24) TFZP/TGDR lines,
product 395 with supplierinfo 10, VİTEL (434) ManageEngine variants. SQLite schema from the real
models; PostgreSQL-only behaviour (partial index, advisory lock) is covered in
``tests/integration/test_pr_f2_advisory_postgres.py``.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.advisory.product_suggestions import (
    ENGINE_VERSION,
    CatalogSnapshot,
    CatalogSupplierMapping,
    CatalogVariant,
    PriorMapping,
    SuggestionPolicy,
    SuggestionTier,
    catalog_fingerprint,
    combine_fingerprints,
    suggest_products,
    supplierinfo_fingerprint,
    variant_catalog_fingerprint,
)
from app.advisory.suggestion_sets import (
    DISPLAY_VALIDITY,
    AdvisoryLine,
    SuggestionSetReason,
    SuggestionSetStatus,
    is_displayable,
)
from app.application.workbench.dto import ReviewStatus
from app.application.workbench.product_line_projection import product_line_key
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode
from app.cli import advisory_product_suggestions as cli
from app.composition import advisory as composition
from app.core.config import Settings
from app.core.runtime_checks import runtime_configuration_errors
from app.db.base import Base
from app.erp.exceptions import ErpRepositoryTimeoutError
from app.erp.odoo.advisory_catalog_reader import AdvisoryCatalogBoundExceededError, OdooAdvisoryCatalogReader
from app.matching import PartnerMatchStatus, ProductMatchStatus
from app.models.workbench_product_line_creation import WorkbenchProductLineCreation
from app.models.workbench_product_line_suggestion import (
    SUGGESTION_SET_REASONS,
    SUGGESTION_SET_STATUSES,
    SUGGESTION_TIERS,
    WorkbenchProductLineSuggestionCandidate,
    WorkbenchProductLineSuggestionSet,
)
from app.persistence.product_line_suggestion_repository import SqlAlchemyProductLineSuggestionRepository
from app.services.advisory_product_suggestions import (
    AdvisoryProductSuggestionService,
    AdvisoryTickStatus,
    HubLineSnapshot,
    PlannedAction,
)

COMPANY = 1
ICT_BULUT, VITEL, OTHER = 24, 434, 77
T_START = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
POLICY = SuggestionPolicy(manual_only_supplier_ids=frozenset({VITEL}), manual_only_category_ids=frozenset({5}))
REVIEW_A = "review:3943f7f9-c038-5154-8f8c-5657ff12e347"
REVIEW_B = "review:1c825f3b-3cec-54be-bcb0-22d75feb50d4"
REVIEW_C = "review:b7c4b622-5707-5b46-8ec7-b8e83d28e2cd"
ADVISORY_TABLES = {"workbench_product_line_suggestion_sets", "workbench_product_line_suggestion_candidates"}


def _variant(product_id, name, *, template_id=None, code=None, category=None, company=None):
    return CatalogVariant(
        product_id=product_id,
        template_id=template_id if template_id is not None else product_id + 10_000,
        name=name,
        default_code=code,
        active=True,
        purchase_ok=True,
        category_id=category,
        company_id=company,
    )


WINDOWS_VM = _variant(395, "Microsoft Windows Server Standard (per VM)", template_id=164, category=44)
ME = _variant(
    392, "[85710.1S1] ManageEngine Endpoint Central (50 Endpoints)", template_id=900, code="85710.1S1", category=7
)
NOISE = _variant(156, "Booking Fees")
SI_TFZP = CatalogSupplierMapping(supplierinfo_id=10, partner_id=ICT_BULUT, product_code="TFZP", template_id=164)
CATEGORIES = ((5, "Software Licenses / ManageEngine"), (7, "Software Licenses / ManageEngine / Endpoint Management"))


def _catalog(*extra, mappings=(SI_TFZP,)) -> CatalogSnapshot:
    return CatalogSnapshot(
        variants=(WINDOWS_VM, ME, NOISE, *extra), supplier_mappings=tuple(mappings), category_paths=CATEGORIES
    )


def _line(review_id, version, number, *, supplier=ICT_BULUT, code=None, description=None, manufacturer=None):
    return AdvisoryLine(
        company_id=COMPANY,
        review_id=review_id,
        review_version=version,
        line_number=number,
        line_key=product_line_key(company_id=COMPANY, review_id=review_id, line_number=number),
        line_sequence=int(number),
        supplier_partner_id=supplier,
        seller_item_code=code,
        description=description,
        manufacturer_item_code=manufacturer,
    )


TFZP = _line(REVIEW_A, 2, "1", code="TFZP", description="MS Windows Server Standart Edition Lisans per VM")
TGDR = _line(REVIEW_A, 2, "2", code="TGDR", description="MS SQL Server Standart Edition 2 Core Lisans")
VITEL_ME = _line(REVIEW_B, 1, "2", supplier=VITEL, code="1531016053", description="85710.1SS3")
GENERIC = _line(REVIEW_C, 2, "1", code="01.0001", description="CLOUD HİZMETİ")


class _Clock:
    def __init__(self, now: datetime = T_START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class _Lines:
    def __init__(self, lines, pending=None) -> None:
        self.lines = list(lines)
        self.pending = pending

    def snapshot(self, *, company_id: int) -> HubLineSnapshot:
        pending = (
            self.pending if self.pending is not None else {line.review_id: line.review_version for line in self.lines}
        )
        return HubLineSnapshot(lines=tuple(self.lines), pending_versions=dict(pending))


class _Priors:
    def __init__(self, priors=()) -> None:
        self.priors = tuple(priors)

    def prior_mappings(self, *, company_id: int):
        return self.priors


class _Catalog:
    def __init__(self, catalog: CatalogSnapshot) -> None:
        self.catalog = catalog
        self.calls = 0
        self.error: Exception | None = None

    def read(self, *, company_id: int) -> CatalogSnapshot:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.catalog


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hub.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    yield engine, factory
    engine.dispose()


def _service(session, lines, catalog, clock, **kwargs) -> AdvisoryProductSuggestionService:
    return AdvisoryProductSuggestionService(
        line_source=lines,
        prior_source=kwargs.pop("priors", _Priors()),
        catalog_reader=catalog,
        repository=kwargs.pop("repository", SqlAlchemyProductLineSuggestionRepository(session)),
        commit=session.commit,
        rollback=session.rollback,
        policy=POLICY,
        clock=clock,
        **kwargs,
    )


def _sets(session):
    return session.scalars(
        select(WorkbenchProductLineSuggestionSet).order_by(WorkbenchProductLineSuggestionSet.id)
    ).all()


def _current(session):
    return {
        (row.review_id, row.review_version, row.line_number): row
        for row in _sets(session)
        if row.status == SuggestionSetStatus.CURRENT.value
    }


def _counts(engine):
    with engine.connect() as connection:
        return {
            table.name: connection.scalar(select(func.count()).select_from(table))
            for table in Base.metadata.sorted_tables
        }


# ------------------------------------------------------------------------------ schema / migration


def test_model_enumerations_match_the_engine_and_lifecycle_enums() -> None:
    assert SUGGESTION_TIERS == tuple(tier.value for tier in SuggestionTier)
    assert SUGGESTION_SET_STATUSES == tuple(status.value for status in SuggestionSetStatus)
    assert SUGGESTION_SET_REASONS == tuple(reason.value for reason in SuggestionSetReason)


def test_partial_unique_index_allows_one_current_set_per_line_version(db) -> None:
    engine, factory = db
    with factory() as session:
        repository = SqlAlchemyProductLineSuggestionRepository(session)
        first = suggest_products(TFZP.suggestion_line(), _catalog(), policy=POLICY)
        kwargs = dict(
            catalog_fingerprint="sha256:a", supplierinfo_fingerprint="sha256:b", engine_version="x", now=T_START
        )
        set_id = repository.insert_current(TFZP, first, **kwargs)
        session.commit()
        with pytest.raises(IntegrityError):
            repository.insert_current(TFZP, first, **kwargs)
        session.rollback()
        # A retired set frees the slot; history of the same line version is kept.
        assert repository.retire(
            set_id, status=SuggestionSetStatus.SUPERSEDED, reason=SuggestionSetReason.CATALOG_CHANGED, now=T_START
        )
        repository.insert_current(TFZP, first, **kwargs)
        session.commit()
        assert [row.status for row in _sets(session)] == ["superseded", "current"]
    unique = {
        index["name"]
        for index in inspect(engine).get_indexes(WorkbenchProductLineSuggestionSet.__tablename__)
        if index["unique"]
    }
    assert "ux_wpls_current_line" in unique


def test_status_reason_check_rejects_a_current_row_with_a_reason(db) -> None:
    _engine, factory = db
    with factory() as session:
        session.add(
            WorkbenchProductLineSuggestionSet(
                company_id=1, review_id=REVIEW_A, review_version=2, line_number="1", line_key="k", input_digest="d",
                catalog_fingerprint="c", supplierinfo_fingerprint="s", engine_version="e", ambiguous=False,
                reclassification_recommended=False, notes=[], status="current", status_reason="line_resolved",
                generated_at=T_START, validated_at=T_START,
            )
        )  # fmt: skip
        with pytest.raises(IntegrityError):
            session.commit()


# ------------------------------------------------------------------------------ fingerprints


def test_fingerprints_are_order_independent_and_stable() -> None:
    catalog = _catalog(mappings=(SI_TFZP, CatalogSupplierMapping(11, ICT_BULUT, "TGDR", 164)))
    reordered = CatalogSnapshot(
        variants=tuple(reversed(catalog.variants)),
        supplier_mappings=tuple(reversed(catalog.supplier_mappings)),
        category_paths=tuple(reversed(catalog.category_paths)),
    )
    assert variant_catalog_fingerprint(reordered) == variant_catalog_fingerprint(catalog)
    assert supplierinfo_fingerprint(reordered) == supplierinfo_fingerprint(catalog)
    assert catalog_fingerprint(catalog) == combine_fingerprints(
        variant_catalog_fingerprint(catalog), supplierinfo_fingerprint(catalog)
    )
    assert variant_catalog_fingerprint(catalog) == variant_catalog_fingerprint(_catalog(mappings=()))


@pytest.mark.parametrize(
    "change",
    [
        {"product_id": 999},
        {"template_id": 1},
        {"name": "Microsoft Windows Server Standard (per VM, 2026)"},
        {"default_code": "MS-WS-STD"},
        {"category_id": 4},
        {"active": False},
        {"purchase_ok": False},
        {"company_id": 2},
    ],
)
def test_catalog_fingerprint_covers_every_variant_fact(change) -> None:
    changed = CatalogSnapshot(variants=(replace(WINDOWS_VM, **change), ME, NOISE), supplier_mappings=(SI_TFZP,))
    base = CatalogSnapshot(variants=(WINDOWS_VM, ME, NOISE), supplier_mappings=(SI_TFZP,))
    assert variant_catalog_fingerprint(changed) != variant_catalog_fingerprint(base)
    assert supplierinfo_fingerprint(changed) == supplierinfo_fingerprint(base)


@pytest.mark.parametrize(
    "change",
    [
        {"supplierinfo_id": 99},
        {"partner_id": 25},
        {"template_id": 165},
        {"product_id": 395},
        {"product_code": "TFZQ"},
        {"company_id": 1},
    ],
)
def test_supplierinfo_fingerprint_covers_every_mapping_fact(change) -> None:
    assert supplierinfo_fingerprint(_catalog(mappings=(replace(SI_TFZP, **change),))) != supplierinfo_fingerprint(
        _catalog()
    )
    assert variant_catalog_fingerprint(_catalog(mappings=(replace(SI_TFZP, **change),))) == variant_catalog_fingerprint(
        _catalog()
    )


# ------------------------------------------------------------------------------ worker lifecycle


def test_first_tick_generates_production_shaped_golden_sets(db) -> None:
    _engine, factory = db
    catalog = _Catalog(_catalog())
    with factory() as session:
        result = _service(session, _Lines([TFZP, TGDR, VITEL_ME, GENERIC]), catalog, _Clock()).run(company_id=COMPANY)
        assert result.status is AdvisoryTickStatus.COMPLETED
        assert (result.generated, result.revalidated, result.failed_lines) == (4, 0, 0)
        assert catalog.calls == 1
        current = _current(session)
        tfzp = current[TFZP.key]
        assert tfzp.reclassification_recommended is True
        assert tfzp.validated_at == T_START == tfzp.generated_at
        assert tfzp.supplier_partner_id == ICT_BULUT and tfzp.engine_version == ENGINE_VERSION
        candidates = session.scalars(
            select(WorkbenchProductLineSuggestionCandidate).where(
                WorkbenchProductLineSuggestionCandidate.set_id == tfzp.id
            )
        ).all()
        assert [(c.product_id, c.tier, c.rank, c.score) for c in candidates] == [
            (395, "t0_stale_supplierinfo", 1, Decimal("1.00"))
        ]
        for line in (TGDR, VITEL_ME, GENERIC):
            stored = current[line.key]
            assert (
                session.scalar(select(func.count()).where(WorkbenchProductLineSuggestionCandidate.set_id == stored.id))
                == 0
            ), line


def test_unchanged_sets_are_only_revalidated(db) -> None:
    _engine, factory = db
    clock = _Clock()
    with factory() as session:
        service = _service(session, _Lines([TFZP, GENERIC]), _Catalog(_catalog()), clock)
        service.run(company_id=COMPANY)
        clock.advance(minutes=15)
        result = service.run(company_id=COMPANY)
        assert (result.generated, result.revalidated) == (0, 2)
        rows = _sets(session)
        assert len(rows) == 2 and all(row.validated_at == clock.now for row in rows)
        assert all(row.generated_at == T_START for row in rows)


@pytest.mark.parametrize(
    ("catalog_after", "reason"),
    [
        (_catalog(_variant(901, "New Product")), SuggestionSetReason.CATALOG_CHANGED),
        (
            _catalog(mappings=(SI_TFZP, CatalogSupplierMapping(11, ICT_BULUT, "TGDR", 164))),
            SuggestionSetReason.SUPPLIERINFO_CHANGED,
        ),
    ],
)
def test_catalog_and_supplierinfo_changes_supersede_and_regenerate(db, catalog_after, reason) -> None:
    _engine, factory = db
    catalog = _Catalog(_catalog())
    with factory() as session:
        service = _service(session, _Lines([TGDR]), catalog, _Clock())
        service.run(company_id=COMPANY)
        catalog.catalog = catalog_after
        result = service.run(company_id=COMPANY)
        assert result.generated == 1
        old, new = _sets(session)
        assert (old.status, old.status_reason) == ("superseded", reason.value)
        assert new.status == "current"


def test_new_supplierinfo_turns_a_line_into_a_t0_stale_mapping(db) -> None:
    _engine, factory = db
    catalog = _Catalog(_catalog(mappings=()))
    with factory() as session:
        service = _service(session, _Lines([TGDR]), catalog, _Clock())
        service.run(company_id=COMPANY)
        assert _current(session)[TGDR.key].reclassification_recommended is False
        catalog.catalog = _catalog(mappings=(CatalogSupplierMapping(11, ICT_BULUT, "TGDR", 164),))
        service.run(company_id=COMPANY)
        assert _current(session)[TGDR.key].reclassification_recommended is True


def test_engine_version_change_regenerates(db) -> None:
    _engine, factory = db
    with factory() as session:
        repository = SqlAlchemyProductLineSuggestionRepository(session)
        catalog = _catalog()
        repository.insert_current(
            GENERIC,
            suggest_products(GENERIC.suggestion_line(), catalog, policy=POLICY),
            catalog_fingerprint=variant_catalog_fingerprint(catalog),
            supplierinfo_fingerprint=supplierinfo_fingerprint(catalog),
            engine_version="f0.9",
            now=T_START,
        )
        session.commit()
        _service(session, _Lines([GENERIC]), _Catalog(catalog), _Clock()).run(company_id=COMPANY)
        old, new = _sets(session)
        assert (old.status_reason, new.engine_version) == ("engine_changed", ENGINE_VERSION)


def test_line_input_change_regenerates(db) -> None:
    _engine, factory = db
    lines = _Lines([GENERIC])
    with factory() as session:
        service = _service(session, lines, _Catalog(_catalog()), _Clock())
        service.run(company_id=COMPANY)
        lines.lines = [replace(GENERIC, description="CLOUD HİZMETİ (düzeltilmiş)")]
        service.run(company_id=COMPANY)
        assert [row.status_reason for row in _sets(session)] == ["input_changed", None]


def test_review_version_change_supersedes_the_old_version_and_generates_the_new(db) -> None:
    _engine, factory = db
    lines = _Lines([TGDR])
    with factory() as session:
        service = _service(session, lines, _Catalog(_catalog()), _Clock())
        service.run(company_id=COMPANY)
        lines.lines = [replace(TGDR, review_version=3)]
        result = service.run(company_id=COMPANY)
        assert (result.retired, result.generated) == (1, 1)
        old, new = _sets(session)
        assert (old.review_version, old.status, old.status_reason) == (2, "superseded", "review_version_changed")
        assert (new.review_version, new.status) == (3, "current")


@pytest.mark.parametrize(
    ("pending", "reason"),
    [({REVIEW_A: 2}, "line_resolved"), ({}, "review_not_pending")],
)
def test_resolved_lines_and_closed_reviews_are_invalidated_even_when_odoo_fails(db, pending, reason) -> None:
    _engine, factory = db
    lines = _Lines([TGDR, GENERIC])
    catalog = _Catalog(_catalog())
    with factory() as session:
        service = _service(session, lines, catalog, _Clock())
        service.run(company_id=COMPANY)
        lines.lines, lines.pending = [GENERIC], {**pending, REVIEW_C: 2}
        catalog.error = ErpRepositoryTimeoutError("Odoo request timed out.")
        result = service.run(company_id=COMPANY)
        assert result.status is AdvisoryTickStatus.CATALOG_FAILED and result.retired == 1
        tgdr = next(row for row in _sets(session) if row.line_number == "2")
        assert (tgdr.status, tgdr.status_reason) == ("invalidated", reason)


# ------------------------------------------------------------------------------ Odoo failure and freshness


@pytest.mark.parametrize(
    "error",
    [
        ErpRepositoryTimeoutError("Odoo request timed out."),
        AdvisoryCatalogBoundExceededError("too many"),
        RuntimeError("unexpected"),
    ],
)
def test_catalog_failure_preserves_evidence_and_never_revalidates(db, error) -> None:
    _engine, factory = db
    clock = _Clock()
    catalog = _Catalog(_catalog())
    with factory() as session:
        service = _service(session, _Lines([TFZP, GENERIC]), catalog, clock)
        service.run(company_id=COMPANY)
        before = [(row.id, row.status, row.validated_at) for row in _sets(session)]
        catalog.error = error
        clock.advance(minutes=15)
        result = service.run(company_id=COMPANY)
        assert result.status is AdvisoryTickStatus.CATALOG_FAILED
        assert (result.generated, result.revalidated) == (0, 0) and result.errors
        assert [(row.id, row.status, row.validated_at) for row in _sets(session)] == before


def test_display_validity_is_twenty_minutes_from_the_last_successful_validation(db) -> None:
    _engine, factory = db
    clock = _Clock()
    catalog = _Catalog(_catalog())
    with factory() as session:
        repository = SqlAlchemyProductLineSuggestionRepository(session)
        service = _service(session, _Lines([TFZP]), catalog, clock, repository=repository)
        service.run(company_id=COMPANY)
        (stored,) = repository.list_current(company_id=COMPANY)
        assert DISPLAY_VALIDITY == timedelta(minutes=20)
        assert is_displayable(stored, now=T_START + timedelta(minutes=20))
        assert not is_displayable(stored, now=T_START + timedelta(minutes=20, seconds=1))
        catalog.error = ErpRepositoryTimeoutError("down")
        clock.advance(minutes=15)
        service.run(company_id=COMPANY)
        (still,) = repository.list_current(company_id=COMPANY)
        assert still.validated_at == T_START
        assert repository.displayable(
            company_id=COMPANY, review_id=REVIEW_A, review_version=2, now=T_START + timedelta(minutes=19)
        )
        assert not repository.displayable(
            company_id=COMPANY, review_id=REVIEW_A, review_version=2, now=T_START + timedelta(minutes=21)
        )
        catalog.error = None
        clock.advance(minutes=10)
        service.run(company_id=COMPANY)
        assert repository.displayable(company_id=COMPANY, review_id=REVIEW_A, review_version=2, now=clock.now)


def test_no_unmatched_lines_means_no_odoo_call(db) -> None:
    _engine, factory = db
    catalog = _Catalog(_catalog())
    with factory() as session:
        result = _service(session, _Lines([]), catalog, _Clock()).run(company_id=COMPANY)
    assert result.status is AdvisoryTickStatus.NO_LINES and catalog.calls == 0


def test_line_cap_and_time_budget_are_enforced(db) -> None:
    _engine, factory = db
    ticks = iter([0.0, 1.0, 31.0, 31.0, 31.0])
    with factory() as session:
        capped = _service(session, _Lines([TFZP, TGDR, GENERIC]), _Catalog(_catalog()), _Clock(), max_lines=2)
        assert capped.run(company_id=COMPANY).truncated == 1
    with factory() as session:
        budget = _service(
            session, _Lines([VITEL_ME, replace(GENERIC, line_number="9")]), _Catalog(_catalog()), _Clock(),
            monotonic=lambda: next(ticks),
        )  # fmt: skip
        result = budget.run(company_id=COMPANY)
        assert result.status is AdvisoryTickStatus.BUDGET_EXHAUSTED and result.generated == 1


def test_a_failing_line_is_rolled_back_and_the_others_continue(db) -> None:
    _engine, factory = db
    with factory() as session:
        repository = SqlAlchemyProductLineSuggestionRepository(session)
        original = repository.insert_current

        def flaky(line, result, **kwargs):
            if line.line_number == "2":
                raise RuntimeError("disk full")
            return original(line, result, **kwargs)

        repository.insert_current = flaky  # type: ignore[method-assign]
        result = _service(
            session, _Lines([TFZP, TGDR, GENERIC]), _Catalog(_catalog()), _Clock(), repository=repository
        ).run(company_id=COMPANY)
        assert (result.generated, result.failed_lines) == (2, 1)
        assert set(_current(session)) == {TFZP.key, GENERIC.key}


def test_history_mapping_feeds_t2(db) -> None:
    _engine, factory = db
    prior = PriorMapping(supplier_partner_id=ICT_BULUT, product_id=395, description=GENERIC.description, source="x")
    with factory() as session:
        _service(session, _Lines([GENERIC]), _Catalog(_catalog()), _Clock(), priors=_Priors([prior])).run(
            company_id=COMPANY
        )
        stored = _current(session)[GENERIC.key]
        tiers = session.scalars(
            select(WorkbenchProductLineSuggestionCandidate.tier).where(
                WorkbenchProductLineSuggestionCandidate.set_id == stored.id
            )
        ).all()
        assert tiers == ["t2_prior_mapping"]


def test_the_worker_touches_only_advisory_tables(db) -> None:
    engine, factory = db
    before = _counts(engine)
    with factory() as session:
        _service(session, _Lines([TFZP, TGDR, VITEL_ME, GENERIC]), _Catalog(_catalog()), _Clock()).run(
            company_id=COMPANY
        )
    after = _counts(engine)
    assert {name for name in after if after[name] != before[name]} == ADVISORY_TABLES


# ------------------------------------------------------------------------------ Hub sources


def test_line_source_selects_exactly_the_projection_product_not_found_lines(monkeypatch) -> None:
    @dataclass(frozen=True)
    class _Source:
        line_number: str
        seller_item_code: str | None
        description: str | None
        quantity: Decimal | None = Decimal("1")
        unit_code: str | None = "C62"
        manufacturer_item_code: str | None = None

    review = SimpleNamespace(
        review_id=REVIEW_A, version=2, invoice_number="ICF2026000008699", status=ReviewStatus.PENDING_REVIEW,
        review_reasons=(ManualReviewReason(ManualReviewReasonCode.PRODUCT_NOT_FOUND, "x", line_number="2"),),
    )  # fmt: skip
    facts = SimpleNamespace(
        source_lines=(
            _Source("1", "TFZP", "MS Windows Server"),
            _Source("2", "TGDR", "MS SQL Server", manufacturer_item_code="M-1"),
        ),
        supplier_match=SimpleNamespace(status=PartnerMatchStatus.MATCHED, partner_id=ICT_BULUT),
        evidence_lines={
            "1": SimpleNamespace(status=ProductMatchStatus.MATCHED, product_id=395, matched_by="supplier_product_code"),
            "2": SimpleNamespace(status=ProductMatchStatus.NOT_FOUND, product_id=None, matched_by=None),
        },
        had_product_reasons=True,
    )  # fmt: skip
    monkeypatch.setattr(composition, "_product_line_read_facts", lambda *args, **kwargs: facts)
    source = composition.HubAdvisoryLineSource.__new__(composition.HubAdvisoryLineSource)
    source._sources = None
    source._reviews = SimpleNamespace(
        list_review_items=lambda query: SimpleNamespace(items=(review,) if query.offset == 0 else (), total_count=1)
    )
    snapshot = source.snapshot(company_id=COMPANY)

    assert snapshot.pending_versions == {REVIEW_A: 2}
    (line,) = snapshot.lines
    assert (line.line_number, line.seller_item_code, line.supplier_partner_id, line.manufacturer_item_code) == (
        "2",
        "TGDR",
        ICT_BULUT,
        "M-1",
    )
    assert line.line_key == product_line_key(company_id=COMPANY, review_id=REVIEW_A, line_number="2")


def test_history_source_reads_completed_creations_and_ledger_evidence(db) -> None:
    _engine, factory = db
    with factory() as session:
        for status, product_id in (("completed", 395), ("create_attempted", None)):
            session.add(
                WorkbenchProductLineCreation(
                    review_id=REVIEW_A, company_id=COMPANY, review_version=2, line_number="1" if product_id else "2",
                    status=status, supplier_partner_id=ICT_BULUT, seller_item_code="TFZP",
                    product_name="Microsoft Windows Server Standard (per VM)", product_kind="service", categ_id=44,
                    uom_id=1, internal_reference=None, intent_digest="d" * 64,
                    supplier_product_name="MS Windows Server Standart Edition Lisans per VM", approved_by="op",
                    create_attempted_at=T_START, product_id=product_id,
                    product_template_id=164 if product_id else None, supplierinfo_id=10 if product_id else None,
                )
            )  # fmt: skip
        session.commit()
        source = composition.HubPriorMappingSource(session)
        (creation,) = source._creations(COMPANY)
        assert (creation.supplier_partner_id, creation.product_id, creation.seller_item_code) == (
            ICT_BULUT,
            395,
            "TFZP",
        )
        assert creation.description == "MS Windows Server Standart Edition Lisans per VM"

        partner = SimpleNamespace(status=PartnerMatchStatus.MATCHED, partner_id=ICT_BULUT)
        line = SimpleNamespace(
            line_number="2", seller_item_code="100021", description="Microsoft 365 Business Standard"
        )

        def evidence(version: int):
            matched = version == 3
            result = SimpleNamespace(
                status=ProductMatchStatus.MATCHED if matched else ProductMatchStatus.NOT_FOUND,
                product_id=394 if matched else None,
                matched_by="supplier_product_code" if matched else None,
            )
            return SimpleNamespace(
                partner_match=partner,
                invoice=SimpleNamespace(lines=(line,)),
                product_match=SimpleNamespace(line_results=(SimpleNamespace(line_number="2", result=result),)),
            )

        source._reviews = SimpleNamespace(
            get_review_execution_evidence=lambda **kwargs: evidence(kwargs["review_version"])
        )
        ledger = SimpleNamespace(id=23, review_id=REVIEW_A, expected_version=2)
        (mapped,) = source._mapped_lines(ledger, COMPANY)
        assert (mapped.supplier_partner_id, mapped.product_id, mapped.seller_item_code) == (ICT_BULUT, 394, "100021")
        assert "#23" in mapped.source


# ------------------------------------------------------------------------------ catalog reader bounds


class _Adapter:
    def __init__(self, sizes) -> None:
        self.sizes = sizes
        self.calls: list[tuple[str, int | None]] = []

    def search_read_all(self, *, model, domain, fields, max_records=None, page_size=None):
        self.calls.append((model, max_records))
        records = {
            "product.product": lambda i: {
                "id": i, "display_name": f"P{i} (F4, 1 Year)", "default_code": None, "product_tmpl_id": [i, "t"],
                "categ_id": False, "company_id": False if i % 2 else [2, "other"], "active": True, "purchase_ok": True,
            },
            "product.supplierinfo": lambda i: {
                "id": i, "partner_id": [24, "p"], "product_code": "C", "product_tmpl_id": [1, "t"],
                "product_id": False, "company_id": False,
            },
            "product.category": lambda i: {"id": i, "complete_name": f"Cat {i}"},
        }[model]  # fmt: skip
        return tuple(records(i) for i in range(1, min(self.sizes[model], max_records) + 1))


def test_catalog_reader_makes_three_bounded_calls_and_filters_foreign_companies() -> None:
    adapter = _Adapter({"product.product": 4, "product.supplierinfo": 2, "product.category": 3})
    snapshot = OdooAdvisoryCatalogReader(adapter=adapter).read(company_id=COMPANY)  # type: ignore[arg-type]

    assert adapter.calls == [("product.product", 5001), ("product.supplierinfo", 5001), ("product.category", 501)]
    assert [variant.product_id for variant in snapshot.variants] == [1, 3]
    assert snapshot.variants[0].name == "P1 (F4, 1 Year)"
    assert len(snapshot.supplier_mappings) == 2 and len(snapshot.category_paths) == 3


@pytest.mark.parametrize("model", ["product.product", "product.supplierinfo", "product.category"])
def test_catalog_reader_fails_closed_beyond_its_bounds(model) -> None:
    sizes = {"product.product": 1, "product.supplierinfo": 1, "product.category": 1, model: 10}
    reader = OdooAdvisoryCatalogReader(
        adapter=_Adapter(sizes),  # type: ignore[arg-type]
        max_variants=5,
        max_supplierinfo=5,
        max_categories=5,
    )
    with pytest.raises(AdvisoryCatalogBoundExceededError):
        reader.read(company_id=COMPANY)


# ------------------------------------------------------------------------------ feature flag


def _worker_settings(**overrides) -> Settings:
    values = dict(
        odoo_workbench_operator_requests_enabled=True,
        odoo_workbench_operator_requests_company_id=1,
        odoo_workbench_projection_publish_enabled=True,
        uyumsoft_inbound_poll_enabled=False,
    )
    values.update(overrides)
    return Settings(**values)


def test_flag_off_registers_no_advisory_task_and_makes_no_call() -> None:
    from app.workers import uyumsoft_inbound_poller

    settings = _worker_settings()
    assert settings.odoo_workbench_product_line_suggestions_enabled is False
    ticks: list[str] = []
    exit_code = uyumsoft_inbound_poller.main(
        ["--once"],
        settings=settings,
        stop_event=threading.Event(),
        cycle_builder=lambda _settings: pytest.fail("polling disabled"),
        operator_request_tick_builder=lambda _settings: lambda: ticks.append("requests"),
        advisory_tick_builder=lambda _settings: pytest.fail("advisory task must not be built with the flag off"),
    )
    assert exit_code == 0 and ticks == ["requests"]


def test_flag_on_registers_the_independent_advisory_task(monkeypatch) -> None:
    from app.workers import uyumsoft_inbound_poller

    settings = _worker_settings(
        odoo_workbench_product_line_projection_enabled=True, odoo_workbench_product_line_suggestions_enabled=True
    )
    registered: list[tuple[str, float]] = []
    real_scheduler = uyumsoft_inbound_poller.PeriodicTaskScheduler

    def recording_scheduler(*, tasks, stop_event):
        registered.extend((task.name, task.interval_seconds) for task in tasks)
        return real_scheduler(tasks=tasks, stop_event=stop_event)

    monkeypatch.setattr(uyumsoft_inbound_poller, "PeriodicTaskScheduler", recording_scheduler)
    ticks: list[str] = []

    def crashing_advisory() -> None:
        ticks.append("advisory")
        raise RuntimeError("advisory failure never stops the worker")

    exit_code = uyumsoft_inbound_poller.main(
        ["--once"],
        settings=settings,
        stop_event=threading.Event(),
        cycle_builder=lambda _settings: pytest.fail("polling disabled"),
        operator_request_tick_builder=lambda _settings: lambda: ticks.append("requests"),
        advisory_tick_builder=lambda _settings: crashing_advisory,
    )
    assert exit_code == 0 and ticks == ["requests", "advisory"]
    assert registered == [("workbench_operator_requests", 60), ("advisory_product_suggestions", 900)]


def test_flag_on_requires_operator_requests_and_line_projection() -> None:
    errors = runtime_configuration_errors(Settings(odoo_workbench_product_line_suggestions_enabled=True))
    assert any("SUGGESTIONS_ENABLED=true requires ODOO_WORKBENCH_OPERATOR_REQUESTS_ENABLED" in e for e in errors)
    assert any("requires ODOO_WORKBENCH_PRODUCT_LINE_PROJECTION_ENABLED" in e for e in errors)


# ------------------------------------------------------------------------------ CLI


@pytest.fixture()
def cli_db(db, monkeypatch):
    engine, factory = db
    lines = _Lines(
        [TFZP, TGDR, VITEL_ME, GENERIC, _line(REVIEW_C, 2, "2", supplier=OTHER, description="Contoso Backup Agent")]
    )
    monkeypatch.setattr(composition, "HubAdvisoryLineSource", lambda session: lines)
    monkeypatch.setattr(composition, "HubPriorMappingSource", lambda session: _Priors())
    tie = (_variant(811, "Contoso Backup Agent"), _variant(812, "Contoso Backup Agent"))
    return engine, factory, _Catalog(_catalog(*tie))


def test_cli_dry_run_writes_nothing_and_reports_everything(cli_db) -> None:
    engine, _factory, catalog = cli_db
    before = _counts(engine)
    out = io.StringIO()

    exit_code = cli.main(
        ["generate", "--company", "1"], engine=engine, settings=Settings(), catalog_reader=catalog, out=out
    )

    assert exit_code == 0 and _counts(engine) == before
    report = out.getvalue()
    assert "DRY-RUN (no Hub or Odoo writes)" in report
    assert "t0_stale_supplierinfo score=1.00 product=395" in report and "flags: RECLASSIFY" in report
    assert "flags: AMBIGUOUS" in report and "WARN:" in report and "#1 t3_name_similarity" in report
    assert report.count("no suggestion") == 3
    assert "Summary: generate=5" in report


def test_cli_apply_requires_explicit_confirmation(cli_db) -> None:
    engine, _factory, catalog = cli_db
    before = _counts(engine)
    out = io.StringIO()
    assert (
        cli.main(
            ["generate", "--company", "1", "--apply"],
            engine=engine,
            settings=Settings(),
            catalog_reader=catalog,
            out=out,
        )
        == 2
    )
    assert _counts(engine) == before and "requires --confirm-apply" in out.getvalue()

    assert (
        cli.main(
            ["generate", "--company", "1", "--apply", "--confirm-apply"],
            engine=engine,
            settings=Settings(),
            catalog_reader=catalog,
            out=io.StringIO(),
        )  # fmt: skip
        == 0
    )
    assert _counts(engine)["workbench_product_line_suggestion_sets"] == 5


def test_cli_cleanup_is_dry_run_first_and_keeps_current_and_recent_history(cli_db) -> None:
    engine, factory, catalog = cli_db
    cli.main(
        ["generate", "--company", "1", "--apply", "--confirm-apply"],
        engine=engine,
        settings=Settings(),
        catalog_reader=catalog,
        out=io.StringIO(),
    )
    with factory() as session:
        rows = _sets(session)
        for row, changed in ((rows[0], T_START - timedelta(days=91)), (rows[1], T_START - timedelta(days=10))):
            row.status, row.status_reason, row.status_changed_at = "superseded", "catalog_changed", changed
        session.commit()
    out = io.StringIO()
    assert cli.main(["cleanup", "--company", "1"], engine=engine, now=T_START, out=out, settings=Settings()) == 0
    assert "older than" in out.getvalue() and out.getvalue().rstrip().endswith(": 1")
    assert _counts(engine)["workbench_product_line_suggestion_sets"] == 5
    assert (
        cli.main(
            ["cleanup", "--company", "1", "--apply", "--confirm-apply"],
            engine=engine,
            now=T_START,
            out=io.StringIO(),
            settings=Settings(),
        )
        == 0
    )
    assert _counts(engine)["workbench_product_line_suggestion_sets"] == 4


def test_planned_actions_match_the_tick(db) -> None:
    _engine, factory = db
    with factory() as session:
        service = _service(session, _Lines([TFZP, GENERIC]), _Catalog(_catalog()), _Clock())
        assert [p.action for p in service.plan(company_id=COMPANY).lines] == [PlannedAction.GENERATE] * 2
        service.run(company_id=COMPANY)
        assert [p.action for p in service.plan(company_id=COMPANY).lines] == [PlannedAction.REVALIDATE] * 2
