"""PR D.1: ManageEngine products can never be created through generic Workbench creation.

Production shapes (read-only assessment 2026-10-12): VİTEL (partner 434, VKN 9250020961) is
ICT's ManageEngine distributor; its invoice lines carry VİTEL's own numeric seller codes
(``1531013277`` ...) and the ManageEngine SKU only in the description. Category root 5
"Software Licenses / ManageEngine" (here 51) holds 250 variants, some with single-character
suffixes (``67225.5M``).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.application.workbench import product_line_creation_policy as policy_module
from app.application.workbench.exceptions import ProductRemediationSupplierUnresolvedError
from app.application.workbench.product_line_creation import (
    ProductLineCreateManualOnlyError,
    ProductLineCreatePolicyError,
    ProductLineCreationStatus,
)
from app.application.workbench.product_line_creation_policy import (
    BUILTIN_MANUAL_ONLY_SKU_PATTERNS,
    DESCRIPTION_SKU_TOKEN,
    compile_manual_only_sku_patterns,
    vat_key,
)
from app.application.workbench.product_mapping import MapExistingProductCommand
from app.domain.invoice import Party
from app.erp.exceptions import ErpRepositoryError
from app.erp.models import Partner
from tests.unit.test_pr_d_product_line_creation import (
    ICT_BULUT,
    MANAGEENGINE,
    MANAGEENGINE_ITSM,
    MANAGEENGINE_LOOKALIKE,
    PartnerDirectory,
    World,
)
from tests.unit.test_product_not_found_operator_flow import COMPANY, REVIEW, Harness, _invoice, _line

VITEL = 434
VITEL_CONTACT = 435
VITEL_INVOICE_ADDRESS = 436
VITEL_DUPLICATE = 437
VITEL_VKN = "9250020961"


def _partners() -> PartnerDirectory:
    return PartnerDirectory(
        {
            ICT_BULUT: Partner(
                id=ICT_BULUT, name="ICT Bulut", tax_number="4650459971", active=True, commercial_partner_id=ICT_BULUT
            ),
            VITEL: Partner(id=VITEL, name="Vitel", tax_number=VITEL_VKN, active=True, commercial_partner_id=VITEL),
            VITEL_CONTACT: Partner(
                id=VITEL_CONTACT,
                name="Vitel, Satış",
                tax_number=None,
                active=True,
                parent_id=VITEL,
                commercial_partner_id=VITEL,
            ),
            VITEL_INVOICE_ADDRESS: Partner(
                id=VITEL_INVOICE_ADDRESS,
                name="Vitel, Fatura Adresi",
                tax_number=VITEL_VKN,
                active=True,
                parent_id=VITEL,
                commercial_partner_id=VITEL,
            ),
            VITEL_DUPLICATE: Partner(
                id=VITEL_DUPLICATE,
                name="VITEL A.Ş.",
                tax_number="TR 9250020961",
                active=False,
                commercial_partner_id=437,
            ),
        }
    )


def _vitel_invoice(*lines, supplier_vkn: str = VITEL_VKN):
    invoice = _invoice(*lines, number="VIT2026000000123")
    return replace(invoice, supplier=Party(name="VİTEL VERİ İLETİŞİMİ", tax_number=supplier_vkn))


def _world(partner_id: int, *lines, supplier_vkn: str = VITEL_VKN, **kwargs) -> World:
    lines = lines or (_line("1", "1531015246", "Yıllık bakım ve destek hizmeti"),)
    return World(
        invoice=_vitel_invoice(*lines, supplier_vkn=supplier_vkn),
        partner_id=partner_id,
        partners=kwargs.pop("partners", None) or _partners(),
        manual_only_suppliers=kwargs.pop("manual_only_suppliers", (VITEL,)),
        **kwargs,
    )


def _assert_nothing_created(world: World, line: str = "1") -> None:
    assert world.odoo.template_creates == []
    assert world.creation(line) is None
    assert world.harness.reclassify_calls == []
    assert world.harness.consumed == []


# --------------------------------------------------------------------------- supplier protection


@pytest.mark.parametrize(
    "partner_id",
    [VITEL, VITEL_CONTACT, VITEL_INVOICE_ADDRESS],
    ids=["direct-commercial-partner", "child-contact", "invoice-address"],
)
async def test_every_vitel_identity_is_manual_only_even_with_a_neutral_line(partner_id: int) -> None:
    world = _world(partner_id)

    with pytest.raises(ProductLineCreateManualOnlyError, match=rf"tedarikçinin \(#{VITEL}\)"):
        await world.run(world.command())

    _assert_nothing_created(world)


async def test_duplicate_partner_sharing_the_distributor_vat_is_manual_only() -> None:
    world = _world(VITEL_DUPLICATE)

    with pytest.raises(ProductLineCreateManualOnlyError):
        await world.run(world.command())
    _assert_nothing_created(world)


async def test_source_invoice_vat_of_the_distributor_is_manual_only_whatever_partner_resolved() -> None:
    # The effective partner is not VİTEL, but the immutable source invoice says it is.
    world = _world(ICT_BULUT)

    with pytest.raises(ProductLineCreateManualOnlyError):
        await world.run(world.command())
    _assert_nothing_created(world)


def test_vat_comparison_ignores_formatting_and_the_tr_prefix() -> None:
    assert vat_key("TR 925-002-0961") == vat_key("9250020961") == "9250020961"
    assert vat_key(None) is None and vat_key(" ") is None


async def test_non_manageengine_supplier_with_an_ordinary_line_still_creates() -> None:
    world = World(manual_only_suppliers=(VITEL,), partners=_partners())

    result = await world.run(world.command())

    assert result.created_product
    assert world.creation().status is ProductLineCreationStatus.COMPLETED


# --------------------------------------------------------------------------- fail closed


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.partners.pop(ICT_BULUT), "tedarikçi #24 okunamadı"),
        (
            lambda d: d.partners.update({ICT_BULUT: replace(d.partners[ICT_BULUT], commercial_partner_id=None)}),
            "tedarikçi #24 okunamadı",
        ),
        (lambda d: d.partners.pop(VITEL), r"yapılandırılmış tedarikçi \[434\]"),
        (
            lambda d: d.partners.update({ICT_BULUT: replace(d.partners[ICT_BULUT], commercial_partner_id=999)}),
            "ticari tedarikçi #999",
        ),
        (lambda d: setattr(d, "fail", RuntimeError("boom")), "okuma hatası"),
    ],
    ids=["effective-missing", "no-commercial-partner", "configured-missing", "commercial-missing", "reader-crash"],
)
async def test_unverifiable_supplier_identity_refuses_creation(mutate, message: str) -> None:
    partners = _partners()
    mutate(partners)
    world = World(manual_only_suppliers=(VITEL,), partners=partners)

    with pytest.raises(ProductLineCreatePolicyError, match=message):
        await world.run(world.command())
    _assert_nothing_created(world)


async def test_transient_odoo_read_error_propagates_and_creates_nothing() -> None:
    partners = _partners()
    partners.fail = ErpRepositoryError("Odoo request timed out.")
    world = World(manual_only_suppliers=(VITEL,), partners=partners)

    with pytest.raises(ErpRepositoryError):  # RETRY_LATER in the request workflow
        await world.run(world.command())
    _assert_nothing_created(world)


async def test_unresolved_supplier_is_refused_before_any_policy() -> None:
    world = _world(VITEL)
    world.harness.partner_match = None

    with pytest.raises(ProductRemediationSupplierUnresolvedError):
        await world.run(world.command())
    _assert_nothing_created(world)


async def test_configured_suppliers_without_a_reader_fail_closed() -> None:
    world = World(manual_only_suppliers=(VITEL,))
    world.policy._supplier_reader = None

    with pytest.raises(ProductLineCreatePolicyError, match="okuyucu yok"):
        await world.run(world.command())


# --------------------------------------------------------------------------- SKU in the description


@pytest.mark.parametrize(
    ("description", "sku"),
    [
        ("ManageEngine ServiceDesk Plus Enterprise 85710.1SS3 1 Technician", "85710.1SS3"),
        ("67225.5M Endpoint Central Additional Workstation", "67225.5M"),
        ("Lisans (SKU: 67005.6MPMFA4) yıllık", "67005.6MPMFA4"),
        ("Patch Manager Plus - 702012.0sup6", "702012.0sup6"),
    ],
)
async def test_manageengine_sku_in_the_description_blocks_an_unrelated_numeric_seller_code(
    description: str, sku: str
) -> None:
    # Not a manual-only supplier at all: only the description gives it away.
    invoice = _invoice(_line("1", "1531013277", description))
    world = World(invoice=invoice, manual_only_suppliers=(VITEL,), partners=_partners())

    with pytest.raises(ProductLineCreateManualOnlyError, match=f"'{sku}'"):
        await world.run(world.command())
    _assert_nothing_created(world)


@pytest.mark.parametrize(
    "description",
    [
        "Tutar 12500.00 TL",
        "Windows Server 2019 Standard",
        "1000.5 adet kablo",
        "Fatura no 2026.10 dönemi",
        "Model AB12345.6X",
        "Seri AB85710.1SS3",  # glued to preceding code characters: not a standalone SKU
        "Sürüm 85710.1SS3.2",  # continues as a dotted version number
        "Microsoft 365 Business Basic (CFQ7TTC0LH18:0001)",
        "CLOUD HİZMETİ",
        "",
    ],
)
def test_ordinary_descriptions_never_match(description: str) -> None:
    assert DESCRIPTION_SKU_TOKEN.search(description) is None


@pytest.mark.parametrize("code", ["67225.5M", "85610.0N", "85710.1SS3", "6705.5MEE"])
def test_single_character_suffix_seller_codes_are_manual_only(code: str) -> None:
    assert any(pattern.fullmatch(code) for pattern in compile_manual_only_sku_patterns())


@pytest.mark.parametrize("code", ["1531013277", "1500.0", "12500.00", "01.0001", "600.01.02.02.01", "TFZP"])
def test_unrelated_seller_codes_are_not_manual_only(code: str) -> None:
    assert not any(pattern.fullmatch(code) for pattern in compile_manual_only_sku_patterns())


def test_the_original_builtin_shape_is_preserved() -> None:
    assert BUILTIN_MANUAL_ONLY_SKU_PATTERNS[0] == r"^\d{4,6}\.\d[0-9A-Z]{2,8}$"


async def test_sku_policy_evaluation_failure_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_description):
        raise RuntimeError("regex engine exploded")

    monkeypatch.setattr(policy_module, "_description_sku", broken)
    world = World(manual_only_suppliers=(VITEL,), partners=_partners())

    with pytest.raises(ProductLineCreatePolicyError, match="politikası değerlendirilemedi"):
        await world.run(world.command())
    _assert_nothing_created(world)


# --------------------------------------------------------------------------- category ancestry


@pytest.mark.parametrize("category", [MANAGEENGINE, MANAGEENGINE_ITSM])
async def test_manageengine_category_and_descendants_are_refused_for_any_supplier(category: int) -> None:
    world = World(manual_only_suppliers=(VITEL,), partners=_partners())

    with pytest.raises(ProductLineCreateManualOnlyError, match="ManageEngine"):
        await world.run(world.command(categ_id=category))
    _assert_nothing_created(world)


async def test_lookalike_sibling_category_is_allowed() -> None:
    world = World(manual_only_suppliers=(VITEL,), partners=_partners())
    result = await world.run(world.command(categ_id=MANAGEENGINE_LOOKALIKE))
    assert result.created_product


# --------------------------------------------------------------------------- internal reference


async def test_supplier_codes_never_become_internal_references() -> None:
    world = World(manual_only_suppliers=(VITEL,), partners=_partners())
    with pytest.raises(ProductLineCreatePolicyError, match="tedarikçinin ürün koduyla"):
        await world.run(world.command(internal_reference="tfzp"))
    _assert_nothing_created(world)


# --------------------------------------------------------------------------- retry / idempotency


async def test_refused_vitel_request_stays_refused_on_every_retry_and_leaves_no_state() -> None:
    world = _world(VITEL, _line("1", "1531013277", "ServiceDesk Plus 85710.1SS3"))

    for _attempt in range(3):
        with pytest.raises(ProductLineCreateManualOnlyError):
            await world.run(world.command())

    _assert_nothing_created(world)
    assert world.claim("1531013277") is None


async def test_completed_creation_is_still_idempotent_with_the_supplier_guard() -> None:
    world = World(manual_only_suppliers=(VITEL,), partners=_partners())
    await world.run(world.command())

    again = await world.run(world.command(version=2))

    assert again.already_applied and len(world.odoo.template_creates) == 1


# --------------------------------------------------------------------------- PR C is untouched


async def test_existing_product_mapping_still_works_for_vitel_lines() -> None:
    """Manual-only means "create the product in Odoo, then map it" -- mapping must stay open."""

    from tests.unit.test_product_not_found_operator_flow import _matched

    harness = Harness(
        _vitel_invoice(_line("1", "1531013277", "ServiceDesk Plus 85710.1SS3")), partner_match=_matched(VITEL)
    )
    result = await harness.use_case.execute(
        MapExistingProductCommand(
            review_id=REVIEW,
            company_id=COMPANY,
            expected_version=harness.review.version,
            line_number="1",
            product_id=501,
            approved_by="operator",
        )
    )

    assert result.line_resolved and result.supplier_partner_id == VITEL
    (row,) = harness.new_rows()
    assert (row["partner_id"], row["product_code"]) == (VITEL, "1531013277")


def test_composition_wires_the_supplier_guard_from_settings() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.composition.product_remediation import build_create_line_product_use_case
    from app.core.config import Settings
    from tests.unit.test_odoo_supplier_partner_writer import FakeJson2Client

    use_case = build_create_line_product_use_case(
        session=Session(bind=create_engine("sqlite://")),
        settings=Settings(product_creation_manual_only_supplier_ids=frozenset({VITEL})),
        odoo_client=FakeJson2Client(),
    )

    assert use_case._policy._manual_only_supplier_ids == frozenset({VITEL})
    assert type(use_case._policy._supplier_reader).__name__ == "OdooPartnerRepository"


def test_vitel_request_through_the_workbench_pipeline_is_rejected_on_every_submission() -> None:
    from app.application.workbench.operator_request_ingestion import OperatorRequestOutcome
    from tests.unit.test_adr_0013_operator_request_ingestion import FakeLedger
    from tests.unit.test_pr_c_product_line_requests import CHILD, WorkbenchOdoo, _parent
    from tests.unit.test_pr_d_product_line_create_requests import LINE_ROW, NAME, _child, _pipeline

    world = _world(VITEL, _line("1", "1531013277", "ServiceDesk Plus 85710.1SS3"))
    odoo = WorkbenchOdoo(children=[_child()], parents=[_parent()])
    ledger = FakeLedger()

    (first,) = _pipeline(world, odoo, ledger=ledger).run(company_id=COMPANY).results
    odoo.rows[CHILD][LINE_ROW].update(
        {"x_studio_ipp_new_ready": True, "x_studio_ipp_new_requested_at": "2026-10-12 09:05:00"}
    )
    (second,) = _pipeline(world, odoo, ledger=ledger).run(company_id=COMPANY).results

    for result in (first, second):
        assert result.outcome is OperatorRequestOutcome.REJECTED and result.acknowledged
        assert "Workbench'ten yeni ürün oluşturulamaz" in result.message
    assert first.request_key != second.request_key
    assert odoo.rows[CHILD][LINE_ROW]["x_studio_ipp_new_name"] == NAME  # inputs kept for the operator
    _assert_nothing_created(world)
