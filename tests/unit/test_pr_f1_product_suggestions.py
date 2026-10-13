"""PR F / F1: pure advisory product suggestion engine.

Fixtures mirror production shapes read-only on 2026-10-13: ICT Bulut (partner 24) TFZP/TGDR
lines and product 395 with supplierinfo 10, Microsoft 365 variants with CSP Internal References,
ManageEngine variant catalogs (VİTEL, partner 434, category root 5) and Microsoft Fabric
capacity variants. No production record is read or written.
"""

from __future__ import annotations

import ast
import copy
import random
from decimal import Decimal
from pathlib import Path

import pytest

from app.advisory import product_suggestions
from app.advisory.product_suggestions import (
    CatalogSnapshot,
    CatalogSupplierMapping,
    CatalogVariant,
    PriorMapping,
    ProductSuggestionResult,
    SuggestionLine,
    SuggestionPolicy,
    SuggestionTier,
    catalog_fingerprint,
    suggest_products,
)

ICT_BULUT, VITEL, LOGOSOFT, OTHER = 24, 434, 448, 77
POLICY = SuggestionPolicy(manual_only_supplier_ids=frozenset({VITEL}), manual_only_category_ids=frozenset({5}))


def _variant(product_id, name, *, template_id=None, code=None, category=None, active=True, purchase_ok=True):
    return CatalogVariant(
        product_id=product_id,
        template_id=template_id if template_id is not None else product_id + 10_000,
        name=name,
        default_code=code,
        active=active,
        purchase_ok=purchase_ok,
        category_id=category,
    )


WINDOWS_VM = _variant(395, "Microsoft Windows Server Standard (per VM)", template_id=164, category=44)
M365_BASIC = _variant(393, "Microsoft 365 Business Basic", template_id=162, code="CFQ7TTC0LH18:0001", category=41)
M365_STANDARD = _variant(394, "Microsoft 365 Business Standard", template_id=163, code="CFQ7TTC0LDPB:0001", category=41)
ME_TEMPLATE = 900
ME_VARIANTS = (
    _variant(
        392,
        "[85710.1S1] ManageEngine Endpoint Central (50 Endpoints)",
        template_id=ME_TEMPLATE,
        code="85710.1S1",
        category=7,
    ),
    _variant(
        187,
        "[85710.1S3] ManageEngine Endpoint Central (300 Endpoints)",
        template_id=ME_TEMPLATE,
        code="85710.1S3",
        category=7,
    ),
    _variant(
        215,
        "[85510.0SSG] ManageEngine Endpoint Central – Secure Gateway",
        template_id=ME_TEMPLATE,
        code="85510.0SSG",
        category=7,
    ),
    _variant(
        216,
        "[85510.0NSG1] ManageEngine Endpoint Central – Secure Gateway",
        template_id=ME_TEMPLATE,
        code="85510.0NSG1",
        category=7,
    ),
)
FABRIC_TEMPLATE = 143
FABRIC = {
    "f2_payg": _variant(
        145,
        "[AZ-FABRIC-F2-PAYG] Microsoft Fabric Capacity (F2, Pay-as-you-go)",
        template_id=FABRIC_TEMPLATE,
        code="AZ-FABRIC-F2-PAYG",
        category=34,
    ),
    "f4_1y": _variant(
        149,
        "[AZ-FABRIC-F4-RI1Y] Microsoft Fabric Capacity (F4, 1 Year Reservation)",
        template_id=FABRIC_TEMPLATE,
        code="AZ-FABRIC-F4-RI1Y",
        category=34,
    ),
    "f4_3y": _variant(
        150,
        "[AZ-FABRIC-F4-RI3Y] Microsoft Fabric Capacity (F4, 3 Year Reservation)",
        template_id=FABRIC_TEMPLATE,
        code="AZ-FABRIC-F4-RI3Y",
        category=34,
    ),
    "f8_1y": _variant(
        152,
        "[AZ-FABRIC-F8-RI1Y] Microsoft Fabric Capacity (F8, 1 Year Reservation)",
        template_id=FABRIC_TEMPLATE,
        code="AZ-FABRIC-F8-RI1Y",
        category=34,
    ),
    "f8_3y": _variant(
        153,
        "[AZ-FABRIC-F8-RI3Y] Microsoft Fabric Capacity (F8, 3 Year Reservation)",
        template_id=FABRIC_TEMPLATE,
        code="AZ-FABRIC-F8-RI3Y",
        category=34,
    ),
}
NOISE = (
    _variant(156, "Booking Fees"),
    _variant(196, "GPU as a Service", category=39),
    _variant(4, "[ICT-SUPPORT-7X24] ICT - Support Package 7x24", code="ICT-SUPPORT-7X24", category=17),
    _variant(301, "ManageEngine ServiceDesk Plus – Standard (Perpetual)", category=8),
)
CATEGORY_PATHS = (
    (4, "Software Licenses"),
    (5, "Software Licenses / ManageEngine"),
    (7, "Software Licenses / ManageEngine / Endpoint Management"),
    (8, "Software Licenses / ManageEngine / ITSM"),
    (17, "Services / Support & Maintenance"),
    (34, "Cloud Services / Microsoft Azure / Fabric"),
    (39, "Cloud Services / GPU as a Service"),
    (41, "Cloud Services / Microsoft 365"),
    (44, "Software Licenses / Microsoft / Server Licenses"),
)
SUPPLIERINFO_TFZP = CatalogSupplierMapping(
    supplierinfo_id=10, partner_id=ICT_BULUT, product_code="TFZP", template_id=164
)
SUPPLIERINFO_100020 = CatalogSupplierMapping(
    supplierinfo_id=8, partner_id=ICT_BULUT, product_code="100020", template_id=162, product_id=393
)


def _catalog(*extra, mappings=(SUPPLIERINFO_TFZP, SUPPLIERINFO_100020), base=True) -> CatalogSnapshot:
    variants = (WINDOWS_VM, M365_BASIC, M365_STANDARD, *ME_VARIANTS, *FABRIC.values(), *NOISE) if base else ()
    return CatalogSnapshot(
        variants=(*variants, *extra), supplier_mappings=tuple(mappings), category_paths=CATEGORY_PATHS
    )


def _line(description, *, supplier=ICT_BULUT, code=None, manufacturer=None, version=2, line_number="1"):
    return SuggestionLine(
        review_id="review:3943f7f9-c038-5154-8f8c-5657ff12e347",
        company_id=1,
        review_version=version,
        line_number=line_number,
        supplier_partner_id=supplier,
        seller_item_code=code,
        description=description,
        manufacturer_item_code=manufacturer,
    )


def _suggest(line, catalog=None, **kwargs) -> ProductSuggestionResult:
    return suggest_products(line, catalog or _catalog(), policy=kwargs.pop("policy", POLICY), **kwargs)


def _ids(result: ProductSuggestionResult) -> list[int]:
    return [suggestion.product_id for suggestion in result.suggestions]


# ------------------------------------------------------------------------------ T0 stale supplierinfo


def test_tfzp_is_a_stale_deterministic_mapping_to_product_395() -> None:
    catalog = _catalog()
    result = _suggest(_line("MS Windows Server Standart Edition Lisans per VM", code="TFZP", version=2), catalog)

    assert _ids(result) == [395]
    suggestion = result.suggestions[0]
    assert suggestion.tier is SuggestionTier.T0_STALE_SUPPLIERINFO
    assert suggestion.rank == 1 and suggestion.score == Decimal("1.00")
    assert suggestion.review_version == 2 == result.review_version
    assert suggestion.catalog_fingerprint == result.catalog_fingerprint == catalog_fingerprint(catalog)
    assert "#10" in suggestion.reasons[0] and "TFZP" in suggestion.reasons[0]
    assert "yeniden sınıflandırma" in suggestion.warnings[0] and "otomatik yapılmaz" in suggestion.warnings[0]
    assert result.reclassification_recommended is True
    assert result.ambiguous is False


def test_t0_is_supplier_scoped_and_never_borrows_another_suppliers_mapping() -> None:
    result = _suggest(_line("MS Windows Server Standart Edition Lisans per VM", supplier=OTHER, code="TFZP"))

    assert result.reclassification_recommended is False
    assert all(s.tier is not SuggestionTier.T0_STALE_SUPPLIERINFO for s in result.suggestions)


def test_t0_needs_exactly_one_resolvable_supplierinfo_row() -> None:
    duplicate = CatalogSupplierMapping(supplierinfo_id=11, partner_id=ICT_BULUT, product_code="tfzp ", template_id=164)
    result = _suggest(_line("Unrelated text", code="TFZP"), _catalog(mappings=(SUPPLIERINFO_TFZP, duplicate)))

    assert result.suggestions == () and result.reclassification_recommended is False
    assert any("deterministik öneri yok" in note for note in result.notes)


def test_t0_template_level_row_on_a_multi_variant_template_is_not_a_guess() -> None:
    mapping = CatalogSupplierMapping(
        supplierinfo_id=12, partner_id=VITEL, product_code="1531016053", template_id=ME_TEMPLATE
    )
    result = _suggest(_line("85710.1SS3", supplier=VITEL, code="1531016053"), _catalog(mappings=(mapping,)))

    assert result.suggestions == () and result.reclassification_recommended is False


# ------------------------------------------------------------------------------ hard discriminators


def test_tgdr_sql_server_never_suggests_windows_server() -> None:
    result = _suggest(_line("MS SQL Server Standart Edition 2 Core Lisans", code="TGDR"))

    assert 395 not in _ids(result)
    assert result.suggestions == ()


def test_windows_server_line_without_a_supplier_mapping_gets_a_guarded_name_suggestion() -> None:
    result = _suggest(_line("Windows Server Standard lisansı per VM", supplier=OTHER, code="WS-STD"))

    assert _ids(result) == [395]
    suggestion = result.suggestions[0]
    assert suggestion.tier is SuggestionTier.T3_NAME_SIMILARITY
    assert suggestion.score >= Decimal("0.55")
    assert any("windows" in reason for reason in suggestion.reasons)


@pytest.mark.parametrize(
    ("description", "candidate_name"),
    [
        ("Windows Server Standard per VM", "Microsoft Windows Server Datacenter (per VM)"),  # edition
        ("Contoso Backup Agent 5 users", "Contoso Backup Agent (50 users)"),  # capacity
        ("Contoso Backup Agent subscription", "Contoso Backup Agent (Perpetual)"),  # license model
        ("Contoso Backup Agent 1 year", "Contoso Backup Agent (3 Years)"),  # duration
        ("Windows Server Standard 2 Core", "Microsoft Windows Server Standard (per VM)"),  # metric core vs VM
        ("Windows Server Standard per VM SQL", "Microsoft Windows Server Standard (per VM)"),  # family windows+sql
    ],
)
def test_explicit_conflicting_attributes_reject_the_candidate(description: str, candidate_name: str) -> None:
    candidate = _variant(700, candidate_name)
    result = _suggest(_line(description, supplier=OTHER), _catalog(candidate, base=False, mappings=()))

    assert result.suggestions == ()
    assert any("elendi" in note for note in result.notes)


def test_a_different_product_family_never_shares_a_meaningful_token() -> None:
    candidate = _variant(702, "Microsoft Windows Server Standard (per VM)")
    result = _suggest(
        _line("Microsoft SQL Server Standard per VM", supplier=OTHER), _catalog(candidate, base=False, mappings=())
    )

    assert result.suggestions == ()


def test_the_same_attributes_on_both_sides_are_not_a_conflict() -> None:
    candidate = _variant(701, "Contoso Backup Agent (50 users, 1 Year Subscription)")
    result = _suggest(
        _line("Contoso Backup Agent 50 users 1 year subscription", supplier=OTHER),
        _catalog(candidate, base=False, mappings=()),
    )

    assert _ids(result) == [701]


def test_azure_fabric_capacity_and_duration_conflicts_are_rejected() -> None:
    result = _suggest(_line("Microsoft Fabric Capacity F4 1 Year Reservation", supplier=LOGOSOFT, code="X-1"))

    assert _ids(result) == [FABRIC["f4_1y"].product_id]
    for rejected in ("f2_payg", "f4_3y", "f8_1y", "f8_3y"):
        assert FABRIC[rejected].product_id not in _ids(result)


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Microsoft Fabric Capacity F8 3 Year Reservation", "f8_3y"),
        ("Microsoft Fabric Capacity F2 pay-as-you-go", "f2_payg"),
    ],
)
def test_azure_fabric_only_the_exact_capacity_duration_and_model_survives(description: str, expected: str) -> None:
    assert _ids(_suggest(_line(description, supplier=LOGOSOFT))) == [FABRIC[expected].product_id]


# ------------------------------------------------------------------------------ T1 exact identifiers


def test_exact_microsoft_csp_product_and_sku_identifier_is_accepted() -> None:
    fabric_plan = _variant(398, "Azure Fabric Reservation Plan", code="DZH318Z0KCZ7:00KV", category=34)
    result = _suggest(
        _line("Azure Fabric (Reservation)", supplier=LOGOSOFT, code="DZH318Z0KCZ7:00KV"), _catalog(fabric_plan)
    )

    assert result.suggestions[0].product_id == 398
    assert result.suggestions[0].tier is SuggestionTier.T1_EXACT_IDENTIFIER
    assert result.suggestions[0].score == Decimal("1.00")


def test_microsoft_csp_product_id_alone_is_rejected() -> None:
    same_product_other_sku = _variant(397, "Azure Fabric Reservation Plan", code="DZH318Z0KCZ7:0001", category=34)
    result = _suggest(
        _line("Azure Fabric (Reservation)", supplier=LOGOSOFT, code="DZH318Z0KCZ7:00KV"),
        _catalog(same_product_other_sku),
    )

    # Neither as an exact identifier nor through name similarity ("Azure Fabric Reservation Plan").
    assert 397 not in _ids(result)
    assert all(s.tier is not SuggestionTier.T1_EXACT_IDENTIFIER for s in result.suggestions)
    assert any("ProductId" in note for note in result.notes)


def test_a_different_manageengine_sku_is_an_identifier_conflict_for_name_similarity() -> None:
    other_sku = _variant(703, "Contoso Endpoint Agent", code="85710.1S1")
    result = _suggest(
        _line("Contoso Endpoint Agent", supplier=OTHER, code="85710.1S9"), _catalog(other_sku, base=False, mappings=())
    )

    assert result.suggestions == ()


def test_a_plain_supplier_code_is_never_compared_with_the_global_internal_reference() -> None:
    clash = _variant(800, "Unrelated Product", code="TGDR")
    result = _suggest(
        _line("MS SQL Server Standart Edition 2 Core Lisans", supplier=OTHER, code="TGDR"), _catalog(clash)
    )

    assert 800 not in _ids(result)


def test_manufacturer_item_code_is_an_exact_identifier() -> None:
    result = _suggest(_line("anything", supplier=OTHER, manufacturer="CFQ7TTC0LDPB:0001"))

    assert _ids(result)[0] == 394
    assert result.suggestions[0].tier is SuggestionTier.T1_EXACT_IDENTIFIER


# ------------------------------------------------------------------------------ ManageEngine


@pytest.mark.parametrize(
    ("supplier", "code", "description"),
    [
        (VITEL, "1531016053", "85710.1SS3"),
        (VITEL, "1531012111", "85510.0SU2"),
        (VITEL, "1531014743", "85510.0SES3"),
        (OTHER, "X-9", "ManageEngine Endpoint Central 85710.1S2 lisansı"),
        (OTHER, "85710.1S2", "ManageEngine Endpoint Central (50 Endpoints)"),
    ],
)
def test_manageengine_near_skus_are_never_suggested(supplier: int, code: str, description: str) -> None:
    result = _suggest(_line(description, supplier=supplier, code=code))

    assert result.suggestions == ()
    assert not ({variant.product_id for variant in ME_VARIANTS} & set(_ids(result)))


def test_manageengine_exact_description_sku_is_an_exact_identifier() -> None:
    result = _suggest(_line("85710.1S1", supplier=VITEL, code="1531016099"))

    assert _ids(result) == [392]
    assert result.suggestions[0].tier is SuggestionTier.T1_EXACT_IDENTIFIER


def test_manageengine_family_names_never_produce_similarity_suggestions() -> None:
    result = _suggest(_line("ManageEngine Endpoint Central Secure Gateway", supplier=OTHER))

    assert result.suggestions == ()


# ------------------------------------------------------------------------------ T2 earlier Hub mappings


def test_earlier_same_supplier_mapping_is_suggested() -> None:
    prior = PriorMapping(
        supplier_partner_id=ICT_BULUT, product_id=394, description="Microsoft 365 Business Standard", source="ledger 23"
    )
    result = _suggest(_line("Microsoft 365 Business Standard", code="100099"), prior_mappings=(prior,))

    assert result.suggestions[0].product_id == 394
    assert result.suggestions[0].tier is SuggestionTier.T2_PRIOR_MAPPING
    assert "ledger 23" in result.suggestions[0].reasons[0]


def test_earlier_mapping_of_another_supplier_is_ignored() -> None:
    prior = PriorMapping(supplier_partner_id=OTHER, product_id=394, description="CLOUD HİZMETİ")
    result = _suggest(_line("CLOUD HİZMETİ", code="01.0001"), prior_mappings=(prior,))

    assert result.suggestions == ()


# ------------------------------------------------------------------------------ no safe candidate


@pytest.mark.parametrize(
    "description",
    [
        "CLOUD HİZMETİ",
        "Azure plan",
        "Azure Github",
        "İADE",
        "Şarj Hizmet Bedeli (AC)",
        "100G-AOC-5M",
        "RG-RAP72Pro",
        "",
        None,
    ],
)
def test_generic_or_unknown_descriptions_produce_no_suggestion(description) -> None:
    result = _suggest(_line(description, supplier=LOGOSOFT, code="01.0001"))

    assert result.suggestions == ()
    assert result.ambiguous is False and result.reclassification_recommended is False


def test_inactive_unpurchasable_and_foreign_company_products_are_never_suggested() -> None:
    catalog = _catalog(
        _variant(901, "Contoso Backup Agent", active=False),
        _variant(902, "Contoso Backup Agent", purchase_ok=False),
        CatalogVariant(903, 9903, "Contoso Backup Agent", None, True, True, None, company_id=2),
        base=False,
        mappings=(),
    )
    assert _suggest(_line("Contoso Backup Agent", supplier=OTHER), catalog).suggestions == ()


# ------------------------------------------------------------------------------ ranking, ties, limits


def test_ties_share_rank_one_and_are_reported_as_ambiguous() -> None:
    duplicates = (_variant(812, "Contoso Backup Agent"), _variant(811, "Contoso Backup Agent"))
    result = _suggest(_line("Contoso Backup Agent", supplier=OTHER), _catalog(*duplicates, base=False, mappings=()))

    assert _ids(result) == [811, 812]
    assert [s.rank for s in result.suggestions] == [1, 1]
    assert result.ambiguous is True
    assert all("operatör seçmelidir" in s.warnings[0] for s in result.suggestions)


def test_scores_within_the_tie_margin_are_tied_and_beyond_it_are_not() -> None:
    # Generic words change only the character similarity: 1.00, ~0.96 (tied), ~0.86 (not tied).
    names = ("Contoso Backup Agent", "Contoso Backup Agent Plan", "Contoso Backup Agent Cloud Software Package")
    variants = tuple(_variant(820 + i, name) for i, name in enumerate(names))
    result = _suggest(_line("Contoso Backup Agent", supplier=OTHER), _catalog(*variants, base=False, mappings=()))

    by_id = {s.product_id: s for s in result.suggestions}
    assert by_id[820].rank == 1 and by_id[821].rank == 1
    assert Decimal("0") < by_id[820].score - by_id[821].score <= Decimal("0.05")
    assert by_id[822].rank == 3 and by_id[820].score - by_id[822].score > Decimal("0.05")
    assert result.ambiguous is True


def test_at_most_three_suggestions_are_returned() -> None:
    variants = tuple(_variant(830 + i, "Contoso Backup Agent") for i in range(6))
    result = _suggest(_line("Contoso Backup Agent", supplier=OTHER), _catalog(*variants, base=False, mappings=()))

    assert len(result.suggestions) == 3
    assert _ids(result) == [830, 831, 832]
    assert any("6 uygun aday" in note for note in result.notes)


def test_higher_tiers_always_rank_above_name_similarity() -> None:
    exact = _variant(840, "Something Else Entirely", code="ABCDEF123456:0001")
    similar = _variant(841, "Contoso Backup Agent")
    result = _suggest(
        _line("Contoso Backup Agent", supplier=OTHER, code="ABCDEF123456:0001"),
        _catalog(exact, similar, base=False, mappings=()),
    )

    assert [(s.product_id, s.tier, s.rank) for s in result.suggestions] == [
        (840, SuggestionTier.T1_EXACT_IDENTIFIER, 1),
        (841, SuggestionTier.T3_NAME_SIMILARITY, 2),
    ]
    assert result.ambiguous is False


def test_ordering_and_fingerprint_do_not_depend_on_input_order() -> None:
    line = _line("Microsoft Fabric Capacity F4 1 Year Reservation", supplier=LOGOSOFT)
    catalog = _catalog()
    shuffled_variants = list(catalog.variants)
    random.Random(7).shuffle(shuffled_variants)
    shuffled = CatalogSnapshot(
        variants=tuple(shuffled_variants),
        supplier_mappings=tuple(reversed(catalog.supplier_mappings)),
        category_paths=tuple(reversed(catalog.category_paths)),
    )

    assert _suggest(line, shuffled) == _suggest(line, catalog)
    assert catalog_fingerprint(shuffled) == catalog_fingerprint(catalog)


def test_fingerprint_changes_when_the_catalog_changes() -> None:
    assert catalog_fingerprint(_catalog()) != catalog_fingerprint(_catalog(_variant(999, "New Product")))


# ------------------------------------------------------------------------------ advisory only


def test_the_engine_never_mutates_its_inputs() -> None:
    line = _line("MS Windows Server Standart Edition Lisans per VM", code="TFZP")
    catalog = _catalog()
    prior = (PriorMapping(supplier_partner_id=ICT_BULUT, product_id=394, description="x"),)
    before = copy.deepcopy((line, catalog, prior, POLICY))

    first = _suggest(line, catalog, prior_mappings=prior)
    second = _suggest(line, catalog, prior_mappings=prior)

    assert (line, catalog, prior, POLICY) == before
    assert first == second


def test_the_engine_module_has_no_io_or_write_dependencies() -> None:
    tree = ast.parse(Path(product_suggestions.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [None])
        if (node.module if isinstance(node, ast.ImportFrom) else alias.name)
    }
    forbidden = ("httpx", "sqlalchemy", "app.erp", "app.infrastructure", "app.models", "app.connectors", "app.api")
    assert not [module for module in imported if module.startswith(forbidden)]
    source = Path(product_suggestions.__file__).read_text(encoding="utf-8")
    for word in ("datetime", ".write(", ".create(", "search_read", "session"):
        assert word not in source


#: ADR-0014 / F2: the only modules allowed to import ``app.advisory`` -- the advisory worker's own
#: composition, service, repository, read-only catalog reader and CLI. Never matching, decisions,
#: execution, billing, Workbench contracts, operator requests or ERP write paths.
ADVISORY_IMPORTERS = [
    "app/cli/advisory_product_suggestions.py",
    "app/composition/advisory.py",
    "app/erp/odoo/advisory_catalog_reader.py",
    "app/persistence/product_line_suggestion_repository.py",
    "app/services/advisory_product_suggestions.py",
]
#: ... and only the Hub worker and the advisory CLI may reach the advisory composition/service.
ADVISORY_WIRING_IMPORTERS = [
    "app/cli/advisory_product_suggestions.py",
    "app/composition/advisory.py",
    "app/workers/uyumsoft_inbound_poller.py",
]


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _importers(module: str) -> list[str]:
    """Modules outside ``app/advisory`` whose import statements (lazy ones included) name ``module``."""

    app_root = Path(product_suggestions.__file__).resolve().parents[1]
    return sorted(
        path.relative_to(app_root.parent).as_posix()
        for path in app_root.rglob("*.py")
        if "advisory" not in path.relative_to(app_root).parts
        and "__pycache__" not in path.parts
        and any(name == module or name.startswith(module + ".") for name in _imported_modules(path))
    )


def test_no_deciding_or_writing_code_path_imports_the_advisory_package() -> None:
    """ADR-0014: advisory output may only ever be displayed, never feed a decision or a write."""

    assert _importers("app.advisory") == ADVISORY_IMPORTERS
    wiring = sorted(
        set(_importers("app.composition.advisory")) | set(_importers("app.services.advisory_product_suggestions"))
    )
    assert wiring == ADVISORY_WIRING_IMPORTERS


def test_results_carry_structured_evidence_fields() -> None:
    result = _suggest(_line("Windows Server Standard lisansı per VM", supplier=OTHER, version=5))
    suggestion = result.suggestions[0]

    assert set(type(suggestion).__dataclass_fields__) == {
        "rank", "product_id", "tier", "score", "reasons", "warnings", "review_version", "catalog_fingerprint"
    }  # fmt: skip
    assert suggestion.review_version == 5
    assert suggestion.catalog_fingerprint.startswith("sha256:")
