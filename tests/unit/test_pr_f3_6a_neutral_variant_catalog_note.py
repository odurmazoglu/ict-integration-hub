"""PR F / F3.6a: neutral exact-code-only warning (engine f1.3) -- wording only, nothing else moves.

The variant-catalog guard fires for three independent triggers (manual-only supplier, SKU token
in the description, SKU-shaped seller code). Production showed the old "variant license
catalog" wording on VİTEL (434) Ruijie hardware lines. F3.6a replaces the text and bumps the
engine version so persisted sets (and the summaries already published to Odoo) regenerate.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.advisory.product_suggestions import (
    ENGINE_VERSION,
    VARIANT_CATALOG_NOTE,
    SuggestionTier,
    suggest_products,
    supplierinfo_fingerprint,
    variant_catalog_fingerprint,
)
from app.application.workbench.product_line_creation_policy import DESCRIPTION_SKU_TOKEN
from app.composition.advisory_display import MAX_SUMMARY_LENGTH, NO_CANDIDATE_LINE, render_summary
from app.db.base import Base
from app.models.workbench_product_line_suggestion import (
    WorkbenchProductLineSuggestionCandidate,
    WorkbenchProductLineSuggestionSet,
)
from app.persistence.product_line_suggestion_repository import SqlAlchemyProductLineSuggestionRepository
from tests.unit.advisory_golden_corpus import (
    CORPUS,
    F1_2_VARIANT_CATALOG_NOTE,
    load_golden,
    run_corpus,
    snapshot,
)
from tests.unit.test_pr_f1_product_suggestions import OTHER, POLICY, VITEL, _catalog, _line
from tests.unit.test_pr_f2_advisory_suggestion_worker import (
    COMPANY,
    REVIEW_B,
    _Catalog,
    _Clock,
    _Lines,
    _service,
)
from tests.unit.test_pr_f2_advisory_suggestion_worker import _catalog as _f2_catalog
from tests.unit.test_pr_f2_advisory_suggestion_worker import _line as _advisory_line

NEW_TEXT = (
    "Bu tedarikçi veya ürün grubu için benzerlik tabanlı öneri yapılmaz. "
    "Yalnızca kesin ürün kodu eşleşmeleri değerlendirilir."
)
TRIGGERS = ("trigger_manual_only_supplier", "trigger_description_sku_token", "trigger_sku_shaped_seller_code")


# ------------------------------------------------------------------------------ wording + version


def test_note_is_the_approved_neutral_turkish_text() -> None:
    assert VARIANT_CATALOG_NOTE == NEW_TEXT
    lowered = VARIANT_CATALOG_NOTE.casefold()
    assert "lisans" not in lowered and "varyant" not in lowered and "katalog" not in lowered


def test_engine_version_is_bumped_so_persisted_sets_regenerate() -> None:
    assert ENGINE_VERSION == "f1.3"


# ------------------------------------------------------------------------------ the three guard triggers


def _corpus_line(name: str):
    description, supplier, code, manufacturer = CORPUS[name]
    return _line(description, supplier=supplier, code=code, manufacturer=manufacturer)


def test_each_trigger_fires_alone_in_the_corpus() -> None:
    supplier_only = _corpus_line("trigger_manual_only_supplier")
    assert supplier_only.supplier_partner_id in POLICY.manual_only_supplier_ids
    assert not DESCRIPTION_SKU_TOKEN.search(supplier_only.description) and "." not in supplier_only.seller_item_code

    token_only = _corpus_line("trigger_description_sku_token")
    assert token_only.supplier_partner_id not in POLICY.manual_only_supplier_ids
    assert DESCRIPTION_SKU_TOKEN.search(token_only.description) and token_only.seller_item_code is None

    code_only = _corpus_line("trigger_sku_shaped_seller_code")
    assert code_only.supplier_partner_id not in POLICY.manual_only_supplier_ids
    assert not DESCRIPTION_SKU_TOKEN.search(code_only.description)


@pytest.mark.parametrize("name", TRIGGERS)
def test_every_trigger_emits_only_the_neutral_note_and_no_similarity_candidate(name) -> None:
    result = suggest_products(_corpus_line(name), _catalog(), policy=POLICY)

    assert result.suggestions == ()
    assert result.notes == (NEW_TEXT,)
    assert not result.ambiguous and not result.reclassification_recommended


@pytest.mark.parametrize(
    ("name", "control"),
    [
        # same Ruijie description, but a supplier that is not manual-only: the guard stays silent
        ("trigger_manual_only_supplier", {"supplier": OTHER}),
        # same supplier and code, the SKU token removed from the description
        ("trigger_description_sku_token", {"description": "ManageEngine Endpoint Central lisans"}),
        # same description, a seller code that is not SKU-shaped
        ("trigger_sku_shaped_seller_code", {"code": "ENDPOINT-01"}),
    ],
)
def test_without_its_trigger_the_line_gets_no_exact_code_only_note(name, control) -> None:
    description, supplier, code, manufacturer = CORPUS[name]
    values = {"description": description, "supplier": supplier, "code": code, "manufacturer": manufacturer} | control
    line = _line(values.pop("description"), **values)

    assert VARIANT_CATALOG_NOTE not in suggest_products(line, _catalog(), policy=POLICY).notes


def test_vitel_hardware_line_never_gets_a_hardware_fuzzy_candidate() -> None:
    """A Ruijie switch on a manual-only supplier: exact code only, never name similarity."""

    line = _corpus_line("wb20_vitel_ruijie_hardware")
    result = suggest_products(line, _catalog(), policy=POLICY)

    assert line.supplier_partner_id == VITEL
    assert result.suggestions == () and result.notes == (NEW_TEXT,)


# ------------------------------------------------------------------------------ golden parity vs f1.2


def test_only_the_note_text_differs_from_the_frozen_f1_2_engine_output() -> None:
    golden = load_golden()
    current = snapshot(run_corpus())

    assert set(golden) == set(current) == set(CORPUS)
    for name, before in golden.items():
        notes = [NEW_TEXT if note == F1_2_VARIANT_CATALOG_NOTE else note for note in before["notes"]]
        expected = dict(before, notes=notes)
        assert current[name] == expected, name


def test_golden_corpus_covers_every_tier_and_every_safeguard() -> None:
    golden = load_golden()
    tiers = {suggestion["tier"] for case in golden.values() for suggestion in case["suggestions"]}

    assert tiers == {tier.value for tier in SuggestionTier}
    changed = sorted(name for name, case in golden.items() if F1_2_VARIANT_CATALOG_NOTE in case["notes"])
    assert changed == sorted(
        [
            *TRIGGERS,
            "me_exact_sku_t1",
            "wb17_vitel_me_sku_description",
            "wb20_vitel_ruijie_hardware",
            "wb33_vitel_ruijie_ap",
        ]
    )
    # ManageEngine and Microsoft CSP safeguards are part of the frozen corpus and did not move
    assert [s["product_id"] for s in golden["me_exact_sku_t1"]["suggestions"]] == [392]
    assert golden["me_other_sku_conflict"]["suggestions"] == []
    assert [s["product_id"] for s in golden["csp_exact_t1"]["suggestions"]] == [393]
    assert golden["csp_product_id_only"]["suggestions"] == []
    assert golden["wb48_fabric_reservation"]["ambiguous"] is True


# ------------------------------------------------------------------------------ persisted refresh path


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hub.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    yield engine, factory
    engine.dispose()


def test_a_persisted_f1_2_set_is_superseded_with_identical_candidates_and_the_new_note(db) -> None:
    _engine, factory = db
    line = _advisory_line(REVIEW_B, 1, "2", supplier=VITEL, code="1531015246", description="RG-ES205C-P PoE Switch")
    catalog = _f2_catalog()
    fresh = suggest_products(line.suggestion_line(), catalog, policy=POLICY)
    legacy = type(fresh)(
        review_id=fresh.review_id,
        line_number=fresh.line_number,
        review_version=fresh.review_version,
        catalog_fingerprint=fresh.catalog_fingerprint,
        suggestions=fresh.suggestions,
        reclassification_recommended=fresh.reclassification_recommended,
        ambiguous=fresh.ambiguous,
        notes=(F1_2_VARIANT_CATALOG_NOTE,),
    )
    with factory() as session:
        SqlAlchemyProductLineSuggestionRepository(session).insert_current(
            line,
            legacy,
            catalog_fingerprint=variant_catalog_fingerprint(catalog),
            supplierinfo_fingerprint=supplierinfo_fingerprint(catalog),
            engine_version="f1.2",
            now=datetime(2026, 10, 13, 11, 0, tzinfo=UTC),
        )
        session.commit()

        result = _service(session, _Lines([line]), _Catalog(catalog), _Clock()).run(company_id=COMPANY)

        assert (result.generated, result.revalidated, result.failed_lines) == (1, 0, 0)
        old, new = session.scalars(
            select(WorkbenchProductLineSuggestionSet).order_by(WorkbenchProductLineSuggestionSet.id)
        ).all()
        assert (old.status, old.status_reason) == ("superseded", "engine_changed")
        assert old.notes == [F1_2_VARIANT_CATALOG_NOTE]
        assert (new.status, new.engine_version, new.notes) == ("current", "f1.3", [NEW_TEXT])
        assert new.display_synced_at is None  # the display sweep republishes the new wording once
        assert session.scalars(select(WorkbenchProductLineSuggestionCandidate)).all() == []

        # the next tick only revalidates: one regeneration per engine bump, never churn
        again = _service(session, _Lines([line]), _Catalog(catalog), _Clock()).run(company_id=COMPANY)
        assert (again.generated, again.revalidated) == (0, 1)


def test_the_rendered_notes_only_summary_uses_the_new_text(db) -> None:
    _engine, factory = db
    line = _advisory_line(REVIEW_B, 1, "2", supplier=VITEL, code="1531015246", description="RG-ES205C-P PoE Switch")
    catalog = _f2_catalog()
    with factory() as session:
        repository = SqlAlchemyProductLineSuggestionRepository(session)
        set_id = repository.insert_current(
            line,
            suggest_products(line.suggestion_line(), catalog, policy=POLICY),
            catalog_fingerprint=variant_catalog_fingerprint(catalog),
            supplierinfo_fingerprint=supplierinfo_fingerprint(catalog),
            engine_version=ENGINE_VERSION,
            now=datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
        )
        session.commit()
        (stored,) = [item for item in repository.list_current(company_id=COMPANY) if item.id == set_id]

    summary = render_summary(stored)
    assert summary == f"{NO_CANDIDATE_LINE}\nUyarı: {NEW_TEXT}"
    assert len(summary) < MAX_SUMMARY_LENGTH
