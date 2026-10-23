"""V1 product creation completion: explicit sale_ok, purchase_ok=True, Studio UX artifacts.

Builds on the PR D world (tests/unit/test_pr_d_product_line_creation.py): real policy, real
use case, real Odoo writer over an in-memory Odoo, real SQLite persistence.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from lxml import etree

from app.application.exceptions.product_remediation import ProductDataIntegrityError, ProductWriteValidationError
from app.application.workbench.exceptions import ProductRemediationContractError
from app.application.workbench.operator_request_ingestion import OperatorRequestReadFailure
from app.application.workbench.product_line_creation import (
    NewProductIntent,
    NewProductKind,
    ProductLineCreateDuplicateError,
    ProductLineCreateOutcomeUnknownError,
    ProductLineCreateReconciliationRequiredError,
    ProductLineCreationStatus,
)
from app.erp.write.odoo_product_write_policy import OdooProductWritePolicy
from app.erp.write.odoo_product_writer import (
    OdooProductTemplateRepository,
    OdooProductWriter,
)
from app.persistence.workbench_product_line_creation_repository import SqlAlchemyProductLineCreationRepository
from tests.unit.test_pr_d_product_line_create_requests import (
    WorkbenchOdoo,
    _child,
    _parent,
    _read,
)
from tests.unit.test_pr_d_product_line_creation import (
    RESALE_CATEGORY,
    CatalogOdoo,
    World,
    _create_command,
    _creation,
    _intent,
    _resale,
    _session,
)

STUDIO = Path(__file__).resolve().parents[2] / "docs" / "odoo_studio"
ARTIFACTS = STUDIO / "v1_product_lines"


@pytest.fixture
def world():
    w = World()
    yield w
    w.session.close()


def _writer(odoo: CatalogOdoo) -> OdooProductWriter:
    return OdooProductWriter(
        repository=OdooProductTemplateRepository(client=odoo),
        policy=OdooProductWritePolicy(
            product_remediation_write_enabled=True, app_env="staging", odoo_host="test-ictteknoloji.odoo.com"
        ),
    )


# =========================================================================== intent / digest


def test_sale_ok_is_an_explicit_boolean_and_part_of_the_digest() -> None:
    internal = _intent(sale_ok=False)
    resale = _intent(sale_ok=True)
    assert internal.digest() != resale.digest()
    for bad in ("yes", 1, 0):
        with pytest.raises(ProductRemediationContractError, match="Satılabilir"):
            _intent(sale_ok=bad)


def test_pre_v1_intent_digest_is_unchanged() -> None:
    legacy = _intent(sale_ok=None)
    payload = {
        "product_name": legacy.product_name,
        "kind": legacy.kind.value,
        "categ_id": legacy.categ_id,
        "uom_id": legacy.uom_id,
        "internal_reference": legacy.internal_reference,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert legacy.digest() == hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("sale_ok", [True, False, None])
def test_repository_round_trips_sale_ok_and_keeps_legacy_rows_readable(sale_ok) -> None:
    session = _session()
    repository = SqlAlchemyProductLineCreationRepository(session)
    inserted = repository.insert_attempt(_creation(intent=_intent(sale_ok=sale_ok)))
    session.commit()
    found = repository.find(review_id=inserted.review_id, company_id=1, review_version=2, line_number="1")
    assert found is not None and found.intent.sale_ok is sale_ok
    assert found.intent == _intent(sale_ok=sale_ok)


# =========================================================================== writer


async def test_writer_sends_and_reads_back_explicit_sale_and_purchase_flags() -> None:
    odoo = CatalogOdoo()
    await _writer(odoo).create_product(_create_command(sale_ok=False, purchase_ok=True))
    (payload,) = odoo.template_creates
    assert payload["sale_ok"] is False and payload["purchase_ok"] is True
    assert set(payload) == {
        "name",
        "type",
        "uom_id",
        "is_storable",
        "categ_id",
        "seller_ids",
        "sale_ok",
        "purchase_ok",
    }  # no account, tax, company or purpose field
    template = odoo.templates[900]
    assert (template["sale_ok"], template["purchase_ok"]) == (False, True)


async def test_writer_without_flags_keeps_the_pre_v1_payload() -> None:
    odoo = CatalogOdoo()
    await _writer(odoo).create_product(_create_command())
    (payload,) = odoo.template_creates
    assert "sale_ok" not in payload and "purchase_ok" not in payload


async def test_writer_read_back_refuses_a_sale_flag_odoo_did_not_keep() -> None:
    class StubbornOdoo(CatalogOdoo):
        async def create_product_template(self, payload: dict[str, Any]) -> int:
            template_id = await super().create_product_template(payload)
            self.templates[template_id]["sale_ok"] = True  # e.g. an automation re-enables selling
            return template_id

    with pytest.raises(ProductDataIntegrityError, match="sale_ok"):
        await _writer(StubbornOdoo()).create_product(_create_command(sale_ok=False, purchase_ok=True))


def test_flags_are_only_written_on_the_supplier_linked_path() -> None:
    with pytest.raises(ProductWriteValidationError, match="only written with a supplier link"):
        _create_command(supplier_link=None, category=None, sale_ok=False)
    with pytest.raises(ProductWriteValidationError, match="boolean"):
        _create_command(sale_ok="no")


# =========================================================================== use case


async def test_internal_use_service_is_purchasable_and_not_saleable(world: World) -> None:
    result = await world.run(world.command(kind=NewProductKind.SERVICE, sale_ok=False))

    template = world.odoo.templates[result.product_template_id]
    assert (template["type"], template["is_storable"]) == ("service", False)
    assert (template["sale_ok"], template["purchase_ok"]) == (False, True)
    assert world.creation().intent.sale_ok is False
    assert world.creation().status is ProductLineCreationStatus.COMPLETED
    # Master data only: one template create, no other Odoo write, review only reclassified.
    assert len(world.odoo.template_creates) == 1
    assert [call.trigger.value for call in world.harness.reclassify_calls] == ["master_data_changed"]


async def test_resale_product_is_saleable_and_purchasable_on_explicit_choice() -> None:
    world = World(purposes=_resale())
    result = await world.run(world.command(categ_id=RESALE_CATEGORY, kind=NewProductKind.GOODS, sale_ok=True))

    template = world.odoo.templates[result.product_template_id]
    assert (template["sale_ok"], template["purchase_ok"]) == (True, True)
    world.session.close()


async def test_sale_ok_is_never_inferred_from_the_purchase_purpose() -> None:
    world = World(purposes=_resale())  # RESALE purpose, but the operator left "Satılabilir" unchecked
    result = await world.run(world.command(categ_id=RESALE_CATEGORY, kind=NewProductKind.GOODS, sale_ok=False))

    assert world.odoo.templates[result.product_template_id]["sale_ok"] is False
    world.session.close()


async def test_a_new_creation_without_an_explicit_sale_ok_is_refused_before_any_write(world: World) -> None:
    with pytest.raises(ProductRemediationContractError, match="Satılabilir"):
        await world.run(world.command(sale_ok=None))
    assert world.odoo.template_creates == [] and world.creation() is None


async def test_retry_of_a_completed_creation_is_idempotent_and_a_changed_flag_is_refused(world: World) -> None:
    await world.run(world.command(sale_ok=False))

    again = await world.run(world.command(version=2, sale_ok=False))
    assert again.already_applied is True and again.created_product is False
    with pytest.raises(ProductLineCreateDuplicateError):
        await world.run(world.command(version=2, sale_ok=True))
    assert len(world.odoo.template_creates) == 1


async def test_duplicate_name_is_refused_whatever_the_sale_flag(world: World) -> None:
    await world.run(world.command(sale_ok=False))
    # Line 3 (another supplier code) asks for a product with the name just created.
    for sale_ok in (False, True):
        with pytest.raises(ProductLineCreateDuplicateError, match="adlı aktif"):
            await world.run(world.command(line="3", version=3, sale_ok=sale_ok))
    assert len(world.odoo.template_creates) == 1


async def test_interrupted_creation_is_adopted_only_when_the_flags_match(world: World) -> None:
    world.odoo.create_failure = "lost_response"
    with pytest.raises(ProductLineCreateOutcomeUnknownError):
        await world.run(world.command(sale_ok=False))

    result = await world.run(world.command(sale_ok=False))

    assert len(world.odoo.template_creates) == 1  # adopted, never created twice
    assert result.created_product is True
    assert world.creation().status is ProductLineCreationStatus.COMPLETED


async def test_interrupted_creation_whose_odoo_flags_differ_needs_reconciliation(world: World) -> None:
    world.odoo.create_failure = "lost_response"
    with pytest.raises(ProductLineCreateOutcomeUnknownError):
        await world.run(world.command(sale_ok=False))
    world.odoo.templates[900]["sale_ok"] = True  # changed in Odoo before the recovery

    with pytest.raises(ProductLineCreateReconciliationRequiredError):
        await world.run(world.command(sale_ok=False))
    assert world.creation().status is ProductLineCreationStatus.NEEDS_RECONCILIATION
    assert len(world.odoo.template_creates) == 1


async def test_pre_v1_completed_creation_still_replays_for_a_v1_request(world: World) -> None:
    """A creation recorded before sale_ok existed (NULL) is the same request, never a duplicate."""

    legacy = _creation(intent=_intent(sale_ok=None))
    world.repository.insert_attempt(legacy)
    created = world.repository.advance(
        world.creation(),
        expected_status=ProductLineCreationStatus.CREATE_ATTEMPTED,
        new_status=ProductLineCreationStatus.PRODUCT_CREATED,
        product_template_id=800,
        product_id=9800,
    )
    world.repository.advance(
        created,
        expected_status=ProductLineCreationStatus.PRODUCT_CREATED,
        new_status=ProductLineCreationStatus.COMPLETED,
        supplierinfo_id=55,
    )
    world.session.commit()

    again = await world.run(world.command(sale_ok=False))

    assert again.already_applied is True and again.product_template_id == 800
    assert world.odoo.template_creates == []


# =========================================================================== reader


@pytest.mark.parametrize("value", [True, False])
def test_reader_takes_the_studio_checkbox_as_the_explicit_choice(value: bool) -> None:
    odoo = WorkbenchOdoo(children=[_child(x_studio_ipp_new_sale_ok=value)], parents=[_parent()])
    (request,) = _read(odoo)
    assert request.new_product.sale_ok is value
    assert "x_studio_ipp_new_sale_ok" in odoo.reads[0][2]


@pytest.mark.parametrize("value", [None, "Evet", 1])
def test_reader_refuses_an_unreadable_checkbox(value: Any) -> None:
    odoo = WorkbenchOdoo(children=[_child(x_studio_ipp_new_sale_ok=value)], parents=[_parent()])
    (item,) = _read(odoo)
    assert isinstance(item, OperatorRequestReadFailure)
    assert "Satılabilir" in item.message


# =========================================================================== Studio artifacts


def _buttons_provision():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "v1_buttons_provision", STUDIO / "v1_operator_buttons" / "provision.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


PROVISION = _buttons_provision()
SNAPSHOT = (STUDIO / "v1_operator_buttons" / "production_form_combined_20261021.xml").read_text(encoding="utf-8")
UX = (ARTIFACTS / "view_ux.xml").read_text(encoding="utf-8")
ENABLE = (ARTIFACTS / "view_create_enable.xml").read_text(encoding="utf-8")
FIELD = json.loads((ARTIFACTS / "field_new_sale_ok.json").read_text(encoding="utf-8"))
PRODUCT_LINES = "//field[@name='x_studio_ipp_product_line_ids']/list"


def _apply(*extensions: str):
    arch = SNAPSHOT
    tree = None
    for extension in extensions:
        tree = PROVISION.apply_extension(arch, extension)
        arch = etree.tostring(tree, encoding="unicode")
    return tree if tree is not None else etree.fromstring(SNAPSHOT.encode("utf-8"))


def test_ux_view_names_the_mapping_action_and_keeps_creation_hidden() -> None:
    form = _apply(UX)
    (match,) = form.xpath(f"{PRODUCT_LINES}/button[@name='1027']")
    assert match.get("string") == "Mevcut Ürünle Eşleştir"
    assert "not x_studio_ipp_req_product" in match.get("invisible")  # the selected product drives it, unchanged
    (create,) = form.xpath(f"{PRODUCT_LINES}/button[@name='1028']")
    assert create.get("invisible") == "1"  # creation stays off until the activation step
    (hint,) = form.xpath("//div[@name='ipp_v1_product_line_match_hint']")
    assert "Mevcut Ürünle Eşleştir" in hint.text and "gider hesabı burada atanmaz" in hint.text
    assert not form.xpath(f"{PRODUCT_LINES}/field[@name='x_studio_ipp_new_sale_ok']")


def test_activation_view_shows_creation_with_every_guard_and_the_saleable_flag() -> None:
    form = _apply(UX, ENABLE)
    (create,) = form.xpath(f"{PRODUCT_LINES}/button[@name='1028']")
    invisible = create.get("invisible")
    for guard in (
        "x_studio_ipp_match_state != 'product_not_found'",
        "x_studio_ipp_new_ready",
        "x_studio_ipp_req_ready",
        "x_studio_ipp_req_product",
        "not x_studio_ipp_new_name",
        "not x_studio_ipp_new_kind",
        "not x_studio_ipp_new_categ",
        "not x_studio_ipp_new_uom",
    ):
        assert guard in invisible
    confirm = create.get("confirm")
    assert "satın alınabilir" in confirm and "alış faturası oluşturulmaz" in confirm
    names = [node.get("name") for node in form.xpath(f"{PRODUCT_LINES}/*")]
    assert names.index("x_studio_ipp_new_sale_ok") == names.index("x_studio_ipp_new_uom") + 1
    (sale,) = form.xpath(f"{PRODUCT_LINES}/field[@name='x_studio_ipp_new_sale_ok']")
    assert sale.get("readonly") == "x_studio_ipp_match_state != 'product_not_found' or x_studio_ipp_new_ready"
    assert len(form.xpath("//div[@name='ipp_v1_product_line_create_hint']")) == 1


def test_artifacts_are_odoo_19_compatible_and_change_nothing_else() -> None:
    for view in (UX, ENABLE):
        assert "@string" not in view
        assert "attrs=" not in view and "states=" not in view  # removed in Odoo 17+
        assert set(re.findall(r'position="([a-z]+)"', view)) <= {"attributes", "after", "before"}
    before = _apply()
    after = _apply(UX, ENABLE)
    assert len(after.xpath("//header/button")) == len(before.xpath("//header/button"))
    assert len(after.xpath(f"{PRODUCT_LINES}/button")) == len(before.xpath(f"{PRODUCT_LINES}/button")) == 2


def test_saleable_field_defaults_to_false() -> None:
    assert FIELD["name"] == "x_studio_ipp_new_sale_ok" and FIELD["model"] == "x_ipp_wb_product_line"
    assert FIELD["ttype"] == "boolean" and FIELD["required"] is False
    assert "default" not in FIELD  # Odoo boolean default = False
    from app.erp.odoo.workbench_product_line_create_request_reader import OdooProductLineCreateRequestFieldMapping

    assert OdooProductLineCreateRequestFieldMapping.__dataclass_fields__["sale_ok"].default == FIELD["name"]


def test_existing_server_actions_are_not_part_of_this_change() -> None:
    assert not list(ARTIFACTS.glob("**/*.py"))  # 1027 / 1028 code is reused unchanged
    assert NewProductIntent.__dataclass_fields__["sale_ok"].default is None


# =========================================================================== migration 0044


@pytest.fixture
def migrated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from alembic.config import Config
    from sqlalchemy import create_engine

    from app.core.config import get_settings

    url = f"sqlite:///{tmp_path / 'sale-ok.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_engine(url)
    try:
        yield Config("alembic.ini"), engine
    finally:
        engine.dispose()
        get_settings.cache_clear()


def _insert_creation(engine, sale_ok: bool | None) -> None:
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workbench_product_line_creations (review_id, company_id, review_version, line_number, "
                "status, supplier_partner_id, seller_item_code, product_name, product_kind, categ_id, uom_id, "
                "intent_digest, approved_by, create_attempted_at, sale_ok) VALUES ('review:x', 1, 2, '1', "
                "'create_attempted', 24, 'TFZP', 'Ürün', 'service', 6, 1, :digest, 'odoo:2', "
                "'2026-10-23 09:00:00+00:00', :sale_ok)"
            ),
            {"digest": "0" * 64, "sale_ok": sale_ok},
        )


def test_migration_0044_is_additive_and_refuses_a_lossy_downgrade(migrated_db) -> None:
    from sqlalchemy import inspect, text

    from alembic import command

    config, engine = migrated_db
    command.upgrade(config, "head")
    assert "sale_ok" in {column["name"] for column in inspect(engine).get_columns("workbench_product_line_creations")}

    _insert_creation(engine, None)  # a pre-V1 row: downgrade is lossless
    command.downgrade(config, "202607170043")
    assert "sale_ok" not in {
        column["name"] for column in inspect(engine).get_columns("workbench_product_line_creations")
    }
    command.upgrade(config, "head")
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT sale_ok FROM workbench_product_line_creations")) is None
        connection.execute(text("DELETE FROM workbench_product_line_creations"))

    _insert_creation(engine, False)  # an explicit V1 choice: dropping it would orphan the digest
    with pytest.raises(RuntimeError, match="Refusing to drop"):
        command.downgrade(config, "202607170043")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "202607170044"
