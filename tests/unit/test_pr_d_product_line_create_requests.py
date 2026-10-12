"""PR D: new-product requests submitted on Workbench child product line rows.

Reader/acknowledger contract, request shape and key, handler, the REAL ingestion workflow
driving the REAL use case end to end, tick composition (the live PR C channel stays
byte-identical while the create gate is off), startup checks, the read-only Odoo
catalog adapters, and migration 202607170040.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workbench.operator_request_handlers import (
    ProductLineCreateRequestHandler,
    product_creation_message,
)
from app.application.workbench.operator_request_ingestion import (
    ACTION_PERMISSIONS,
    OperatorActor,
    OperatorActorDirectory,
    OperatorRequest,
    OperatorRequestAction,
    OperatorRequestIngestionWorkflow,
    OperatorRequestOutcome,
    OperatorRequestReadFailure,
    operator_request_key,
)
from app.application.workbench.product_line_creation import (
    CreateLineProductResult,
    NewProductIntent,
    NewProductKind,
    ProductLineCreationStatus,
)
from app.application.workbench.product_line_projection import product_line_key
from app.core.config import Settings, get_settings
from app.core.runtime_checks import runtime_configuration_errors
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.product_line_creation_reader import (
    OdooCreatedProductReader,
    OdooProductCatalogReader,
    escape_like,
)
from app.erp.odoo.workbench_product_line_create_request_reader import (
    PENDING_MAPPING_MESSAGE,
    OdooProductLineCreateRequestAcknowledger,
    OdooProductLineCreateRequestFieldMapping,
    OdooProductLineCreateRequestReader,
)
from app.erp.odoo.workbench_product_line_request_reader import (
    IDENTITY_MISMATCH_MESSAGE,
    NOT_CURRENT_MESSAGE,
    PARENT_MISMATCH_MESSAGE,
)
from tests.unit.test_adr_0013_operator_request_ingestion import FakeIssuer, FakeLedger
from tests.unit.test_pr_c_product_line_requests import (
    CHILD,
    PARENT_ROW,
    WorkbenchOdoo,
    _mapping,
    _parent,
    _parent_mapping,
)
from tests.unit.test_pr_d_product_line_creation import SERVICES, UNITS, World
from tests.unit.test_product_not_found_operator_flow import COMPANY, ICT_BULUT, REVIEW, _lines_with_reason

LINE_ROW = 31
OPERATOR_UID = 7
OPERATOR = OperatorActor(actor="operator", permissions=frozenset({"workbench_review_decide", "workbench_execute"}))
SUBMITTED = "2026-10-12 09:00:00"
SUBMITTED_AT = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
NAME = "Windows Server Standard Lisans (VM başına)"


def _create_mapping() -> OdooProductLineCreateRequestFieldMapping:
    return OdooProductLineCreateRequestFieldMapping(line_requests=_mapping())


def _child(line: str = "1", **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": LINE_ROW,
        "x_studio_ipp_workbench_id": [PARENT_ROW, REVIEW],
        "x_studio_ipp_line_key": product_line_key(company_id=COMPANY, review_id=REVIEW, line_number=line),
        "x_studio_ipp_is_current": True,
        "x_studio_ipp_review_id": REVIEW,
        "x_studio_ipp_company_id": COMPANY,
        "x_studio_ipp_review_version": 2,
        "x_studio_ipp_line_number": line,
        "x_studio_ipp_seller_code": "TFZP",
        "x_studio_ipp_description": "MS Windows Server",
        "x_studio_ipp_match_state": "product_not_found",
        "x_studio_ipp_product_id": False,
        "x_studio_ipp_req_product": False,
        "x_studio_ipp_req_ready": False,
        "x_studio_ipp_req_result": False,
        "x_studio_ipp_req_message": False,
        "x_studio_ipp_req_processed_at": False,
        "x_studio_ipp_new_name": NAME,
        "x_studio_ipp_new_kind": "Hizmet",
        "x_studio_ipp_new_categ": [SERVICES, "Services / Implementation"],
        "x_studio_ipp_new_uom": [UNITS, "Units"],
        "x_studio_ipp_new_ref": False,
        "x_studio_ipp_new_ready": True,
        "x_studio_ipp_new_version": 2,
        "x_studio_ipp_new_requested_by": [OPERATOR_UID, "Operator"],
        "x_studio_ipp_new_requested_at": SUBMITTED,
    }
    row.update(overrides)
    return row


def _read(odoo: WorkbenchOdoo):
    return OdooProductLineCreateRequestReader(adapter=odoo, mapping=_create_mapping()).list_pending(
        company_id=COMPANY, limit=25
    )


def _only_failure(odoo: WorkbenchOdoo) -> str:
    (item,) = _read(odoo)
    assert isinstance(item, OperatorRequestReadFailure), item
    return item.message


def _intent(**overrides: Any) -> NewProductIntent:
    values: dict[str, Any] = {
        "product_name": NAME,
        "kind": NewProductKind.SERVICE,
        "categ_id": SERVICES,
        "uom_id": UNITS,
    }
    values.update(overrides)
    return NewProductIntent(**values)


# --------------------------------------------------------------------------- Studio contract


def test_field_defaults_are_the_runbook_contract_and_never_alias_existing_fields() -> None:
    mapping = _create_mapping()
    assert mapping.own_fields() == (
        "x_studio_ipp_new_name",
        "x_studio_ipp_new_kind",
        "x_studio_ipp_new_categ",
        "x_studio_ipp_new_uom",
        "x_studio_ipp_new_ref",
        "x_studio_ipp_new_ready",
        "x_studio_ipp_new_version",
        "x_studio_ipp_new_requested_by",
        "x_studio_ipp_new_requested_at",
    )
    for alias in ("x_studio_ipp_req_ready", "x_studio_ipp_product_id", "x_studio_ipp_req_result"):
        with pytest.raises(WorkbenchContractError, match="overlap"):
            OdooProductLineCreateRequestFieldMapping(line_requests=_mapping(), ready=alias)
    with pytest.raises(WorkbenchContractError, match="distinct"):
        OdooProductLineCreateRequestFieldMapping(line_requests=_mapping(), name="x_studio_ipp_new_ref")


def test_environment_overrides_and_blank_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_WORKBENCH_PRODUCT_LINE_NEW_NAME_FIELD", "x_studio_other_name")
    assert OdooProductLineCreateRequestFieldMapping.from_environment(line_requests=_mapping()).name == (
        "x_studio_other_name"
    )
    monkeypatch.setenv("ODOO_WORKBENCH_PRODUCT_LINE_NEW_READY_FIELD", " ")
    with pytest.raises(WorkbenchContractError, match="NEW_READY_FIELD"):
        OdooProductLineCreateRequestFieldMapping.from_environment(line_requests=_mapping())


def test_reader_never_reads_display_fields_or_the_mapping_product() -> None:
    read = set(_create_mapping().read_fields())
    for display in (
        "x_studio_ipp_seller_code",
        "x_studio_ipp_description",
        "x_studio_ipp_match_state",
        "x_studio_ipp_product_id",
        "x_studio_ipp_review_version",
        "x_studio_ipp_req_product",
    ):
        assert display not in read
    assert "x_studio_ipp_req_ready" in read  # only for the cross-channel conflict check


# --------------------------------------------------------------------------- reader


def test_ready_row_becomes_a_create_request_with_hub_owned_identity() -> None:
    odoo = WorkbenchOdoo(
        children=[_child(x_studio_ipp_seller_code="TAMPERED", x_studio_ipp_new_ref=" ICT-WS ")], parents=[_parent()]
    )

    (request,) = _read(odoo)

    assert request == OperatorRequest(
        odoo_record_id=LINE_ROW,
        review_id=REVIEW,
        company_id=COMPANY,
        action=OperatorRequestAction.PRODUCT_LINE_CREATE,
        expected_version=2,
        requested_by_odoo_user_id=OPERATOR_UID,
        requested_at=SUBMITTED_AT,
        line_number="1",
        new_product=_intent(internal_reference="ICT-WS"),
    )
    assert odoo.reads[0][1] == [["x_studio_ipp_company_id", "=", COMPANY], ["x_studio_ipp_new_ready", "=", True]]
    assert odoo.writes == []


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("Hizmet", NewProductKind.SERVICE),
        ("Stoksuz Mal", NewProductKind.GOODS),
        ("Stoklu Mal", NewProductKind.STOCKED_GOODS),
    ],
)
def test_kind_labels(label: str, kind: NewProductKind) -> None:
    (request,) = _read(WorkbenchOdoo(children=[_child(x_studio_ipp_new_kind=label)], parents=[_parent()]))
    assert request.new_product.kind is kind


def test_mapping_ready_rows_are_not_read_by_the_create_channel() -> None:
    odoo = WorkbenchOdoo(
        children=[_child(x_studio_ipp_new_ready=False, x_studio_ipp_req_ready=True)], parents=[_parent()]
    )
    assert _read(odoo) == ()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"x_studio_ipp_new_requested_at": False}, "Yeni Ürün Oluştur"),
        ({"x_studio_ipp_is_current": False}, NOT_CURRENT_MESSAGE),
        ({"x_studio_ipp_line_number": "2"}, IDENTITY_MISMATCH_MESSAGE),
        ({"x_studio_ipp_workbench_id": [99, "x"]}, PARENT_MISMATCH_MESSAGE),
        ({"x_studio_ipp_req_ready": True}, PENDING_MAPPING_MESSAGE),
        ({"x_studio_ipp_new_name": False}, "Yeni ürün adı girilmelidir."),
        ({"x_studio_ipp_new_name": "İki\nsatır"}, "tek satır"),
        ({"x_studio_ipp_new_kind": False}, "Ürün türü seçilmelidir."),
        ({"x_studio_ipp_new_kind": "Kombo"}, "desteklenmiyor"),
        ({"x_studio_ipp_new_categ": False}, "Ürün kategorisi eksik."),
        ({"x_studio_ipp_new_uom": False}, "Ölçü birimi eksik."),
        ({"x_studio_ipp_new_version": 0}, "İnceleme sürümü geçersiz."),
        ({"x_studio_ipp_new_requested_by": False}, "İsteyen kullanıcı eksik."),
    ],
)
def test_inconsistent_or_incomplete_rows_fail_closed(overrides: dict[str, Any], message: str) -> None:
    assert message in _only_failure(WorkbenchOdoo(children=[_child(**overrides)], parents=[_parent()]))


def test_parent_rows_never_carry_the_create_action() -> None:
    from app.erp.odoo.workbench_operator_request_reader import ACTION_BY_ODOO_VALUE, PARENT_ROW_ACTIONS

    assert OperatorRequestAction.PRODUCT_LINE_CREATE not in PARENT_ROW_ACTIONS
    assert "product_line_create" not in ACTION_BY_ODOO_VALUE


# --------------------------------------------------------------------------- acknowledger


def _ack(odoo: WorkbenchOdoo, outcome: OperatorRequestOutcome, *, clear: bool, requested_at=SUBMITTED_AT) -> bool:
    return OdooProductLineCreateRequestAcknowledger(adapter=odoo, mapping=_create_mapping()).acknowledge(
        odoo_record_id=LINE_ROW,
        requested_at=requested_at,
        outcome=outcome,
        message="ok",
        processed_at=datetime(2026, 10, 12, 9, 1, tzinfo=UTC),
        clear_request_inputs=clear,
    )


def test_completed_acknowledgement_clears_only_the_create_inputs_and_ready_flag() -> None:
    odoo = WorkbenchOdoo(children=[_child(x_studio_ipp_req_product=[502, "x"])], parents=[_parent()])

    assert _ack(odoo, OperatorRequestOutcome.COMPLETED, clear=True)

    ((model, record_id, values),) = odoo.writes
    assert (model, record_id) == (CHILD, LINE_ROW)
    assert values == {
        "x_studio_ipp_req_result": "Tamamlandı",
        "x_studio_ipp_req_message": "ok",
        "x_studio_ipp_req_processed_at": "2026-10-12 09:01:00",
        "x_studio_ipp_new_ready": False,
        "x_studio_ipp_new_name": False,
        "x_studio_ipp_new_kind": False,
        "x_studio_ipp_new_categ": False,
        "x_studio_ipp_new_uom": False,
        "x_studio_ipp_new_ref": False,
    }


def test_rejected_acknowledgement_keeps_inputs_and_a_newer_submission_is_never_overwritten() -> None:
    odoo = WorkbenchOdoo(children=[_child()], parents=[_parent()])
    assert _ack(odoo, OperatorRequestOutcome.REJECTED, clear=False)
    assert "x_studio_ipp_new_name" not in odoo.writes[0][2]
    odoo2 = WorkbenchOdoo(children=[_child()], parents=[_parent()])
    assert not _ack(odoo2, OperatorRequestOutcome.COMPLETED, clear=True, requested_at=SUBMITTED_AT - timedelta(1))
    assert odoo2.writes == []


# --------------------------------------------------------------------------- request shape / key


def test_create_request_needs_an_intent_and_no_existing_product() -> None:
    base = dict(
        odoo_record_id=LINE_ROW,
        review_id=REVIEW,
        company_id=COMPANY,
        action=OperatorRequestAction.PRODUCT_LINE_CREATE,
        expected_version=2,
        requested_by_odoo_user_id=OPERATOR_UID,
        requested_at=SUBMITTED_AT,
        line_number="1",
    )
    with pytest.raises(WorkbenchContractError, match="eksik"):
        OperatorRequest(**base)
    with pytest.raises(WorkbenchContractError, match="mevcut bir ürün"):
        OperatorRequest(**base, new_product=_intent(), product_id=502)
    with pytest.raises(WorkbenchContractError, match="yalnız yeni ürün"):
        OperatorRequest(
            **{**base, "action": OperatorRequestAction.PRODUCT_LINE_MAPPING}, product_id=502, new_product=_intent()
        )
    assert ACTION_PERMISSIONS[OperatorRequestAction.PRODUCT_LINE_CREATE] == frozenset({"workbench_review_decide"})


def test_request_key_binds_the_intent_and_leaves_existing_keys_unchanged() -> None:
    (request,) = _read(WorkbenchOdoo(children=[_child()], parents=[_parent()]))
    other = replace(request, new_product=_intent(categ_id=50))
    assert operator_request_key(request) != operator_request_key(other)
    mapping_request = replace(
        request, action=OperatorRequestAction.PRODUCT_LINE_MAPPING, new_product=None, product_id=502
    )
    assert operator_request_key(mapping_request) != operator_request_key(request)


# --------------------------------------------------------------------------- handler


class _RecordingUseCase:
    def __init__(self, result: CreateLineProductResult) -> None:
        self.result = result
        self.commands: list[Any] = []

    async def execute(self, command):
        self.commands.append(command)
        return self.result


def _result(**overrides: Any) -> CreateLineProductResult:
    values: dict[str, Any] = dict(
        review_id=REVIEW,
        company_id=COMPANY,
        line_number="1",
        seller_item_code="TFZP",
        supplier_partner_id=ICT_BULUT,
        product_name=NAME,
        product_template_id=900,
        product_id=9900,
        supplierinfo_id=104,
        created_product=True,
        already_applied=False,
        previous_version=2,
        current_version=3,
        line_resolved=True,
        remaining_product_lines=("3",),
    )
    values.update(overrides)
    return CreateLineProductResult(**values)


def test_handler_issues_the_create_authorization_and_reports_turkish_results() -> None:
    from app.application.workbench.operator_request_ingestion import OperatorActionContext

    (request,) = _read(WorkbenchOdoo(children=[_child()], parents=[_parent()]))
    use_case = _RecordingUseCase(_result())
    operations: list[str] = []
    context = OperatorActionContext(actor=OPERATOR, ensure_authorization=lambda op: operations.append(op) or "auth-9")

    outcome = ProductLineCreateRequestHandler(use_case=use_case).handle(request, context)

    assert operations == ["CREATE_NEW_PRODUCT"]
    (cmd,) = use_case.commands
    assert (cmd.line_number, cmd.intent, cmd.approved_by, cmd.authorization_id) == (
        "1",
        request.new_product,
        "operator",
        "auth-9",
    )
    assert outcome.outcome is OperatorRequestOutcome.COMPLETED
    assert outcome.message.startswith(f"Yeni ürün oluşturuldu: {NAME} (#9900)")
    assert "Kalan eşleştirilecek satırlar: 3." in outcome.message
    already = ProductLineCreateRequestHandler(use_case=_RecordingUseCase(_result(already_applied=True))).handle(
        request, context
    )
    assert already.outcome is OperatorRequestOutcome.ALREADY_COMPLETED
    assert "teknik destek" in product_creation_message(_result(line_resolved=False))


# --------------------------------------------------------------------------- end to end


class ProjectionRefresh:
    def __init__(self, world: World, odoo: WorkbenchOdoo) -> None:
        self.world = world
        self.odoo = odoo
        self.calls = 0

    def sync(self, *, review_id: str, company_id: int) -> None:
        self.calls += 1
        unmatched = set(_lines_with_reason(self.world.harness.review))
        for row in self.odoo.rows[CHILD].values():
            row["x_studio_ipp_review_version"] = self.world.harness.review.version
            row["x_studio_ipp_match_state"] = (
                "product_not_found" if row["x_studio_ipp_line_number"] in unmatched else "matched"
            )


def _pipeline(world: World, odoo: WorkbenchOdoo, *, actors=None, ledger=None):
    return OperatorRequestIngestionWorkflow(
        reader=OdooProductLineCreateRequestReader(adapter=odoo, mapping=_create_mapping()),
        acknowledger=OdooProductLineCreateRequestAcknowledger(adapter=odoo, mapping=_create_mapping()),
        ledger=ledger or FakeLedger(),
        actors=OperatorActorDirectory({OPERATOR_UID: OPERATOR} if actors is None else actors),
        handlers={OperatorRequestAction.PRODUCT_LINE_CREATE: ProductLineCreateRequestHandler(use_case=world.use_case)},
        authorization_issuer=FakeIssuer(),
        projection_refresher=ProjectionRefresh(world, odoo),
        clock=lambda: datetime(2026, 10, 12, 9, 1, tzinfo=UTC),
        transient_errors=(ErpRepositoryError,),
    )


def test_child_row_request_creates_the_product_matches_the_line_and_clears_the_inputs() -> None:
    world = World()
    odoo = WorkbenchOdoo(children=[_child()], parents=[_parent()])

    (result,) = _pipeline(world, odoo).run(company_id=COMPANY).results

    assert result.outcome is OperatorRequestOutcome.COMPLETED and result.acknowledged
    assert result.action is OperatorRequestAction.PRODUCT_LINE_CREATE
    assert world.odoo.new_templates() == [900]
    row = odoo.rows[CHILD][LINE_ROW]
    assert row["x_studio_ipp_match_state"] == "matched"
    assert row["x_studio_ipp_req_result"] == "Tamamlandı"
    assert (row["x_studio_ipp_new_ready"], row["x_studio_ipp_new_name"], row["x_studio_ipp_new_categ"]) == (
        False,
        False,
        False,
    )
    assert world.creation().status is ProductLineCreationStatus.COMPLETED


def test_crash_before_acknowledgement_only_reacknowledges() -> None:
    world = World()
    odoo = WorkbenchOdoo(children=[_child()], parents=[_parent()])
    ledger = FakeLedger()
    odoo.fail_writes = 1
    (first,) = _pipeline(world, odoo, ledger=ledger).run(company_id=COMPANY).results
    assert first.outcome is OperatorRequestOutcome.COMPLETED and not first.acknowledged

    (second,) = _pipeline(world, odoo, ledger=ledger).run(company_id=COMPANY).results

    assert second.acknowledged and len(world.odoo.template_creates) == 1


def test_operator_without_execute_permission_cannot_create() -> None:
    world = World()
    odoo = WorkbenchOdoo(children=[_child()], parents=[_parent()])
    decider = OperatorActor(actor="decider", permissions=frozenset({"workbench_review_decide"}))

    (result,) = _pipeline(world, odoo, actors={OPERATOR_UID: decider}).run(company_id=COMPANY).results

    assert result.outcome is OperatorRequestOutcome.REJECTED
    assert "workbench_execute" in result.message
    assert world.odoo.template_creates == [] and world.creation() is None


def test_manageengine_line_is_rejected_with_the_manual_only_message() -> None:
    world = World()
    odoo = WorkbenchOdoo(children=[_child(line="2")], parents=[_parent()])

    (result,) = _pipeline(world, odoo).run(company_id=COMPANY).results

    assert result.outcome is OperatorRequestOutcome.REJECTED
    assert "lisans kataloğu" in result.message
    assert odoo.rows[CHILD][LINE_ROW]["x_studio_ipp_new_name"] == NAME  # kept for correction
    assert world.odoo.template_creates == []


# --------------------------------------------------------------------------- configuration and composition


def _ready_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        odoo_workbench_product_line_create_enabled=True,
        odoo_workbench_product_line_requests_enabled=True,
        odoo_workbench_operator_requests_enabled=True,
        odoo_workbench_operator_requests_company_id=1,
        odoo_workbench_projection_publish_enabled=True,
        odoo_workbench_product_line_projection_enabled=True,
        product_creation_uom_ids=frozenset({1}),
        product_creation_manual_only_category_ids=frozenset({51}),
    )
    values.update(overrides)
    return Settings(**values)


def test_create_gate_defaults_off_and_requires_its_prerequisites() -> None:
    assert Settings().odoo_workbench_product_line_create_enabled is False
    assert not any(
        "PRODUCT_LINE_CREATE" in e or "PRODUCT_CREATION" in e for e in runtime_configuration_errors(_ready_settings())
    )
    errors = runtime_configuration_errors(Settings(odoo_workbench_product_line_create_enabled=True))
    assert any("requires ODOO_WORKBENCH_PRODUCT_LINE_REQUESTS_ENABLED=true" in e for e in errors)
    assert any("PRODUCT_CREATION_UOM_IDS" in e for e in errors)
    assert any("PRODUCT_CREATION_MANUAL_ONLY_CATEGORY_IDS" in e for e in errors)
    bad = runtime_configuration_errors(_ready_settings(product_creation_manual_only_sku_patterns=("([",)))
    assert any("PRODUCT_CREATION_MANUAL_ONLY_SKU_PATTERNS" in e for e in bad)


def test_tick_adds_the_create_channel_only_when_enabled_and_keeps_pr_c_unchanged(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from sqlalchemy.orm import Session

    from app.composition import operator_requests
    from app.composition.operator_requests import _tick_workflow
    from tests.unit.test_odoo_supplier_partner_writer import FakeJson2Client

    monkeypatch.setattr(operator_requests, "OdooWorkbenchFieldMapping", MagicMock())
    monkeypatch.setattr(operator_requests, "decision_mapping_for_requests", lambda base, request: MagicMock())
    session = Session(bind=create_engine("sqlite://"))
    common = dict(
        business_session=session,
        ledger_session=session,
        settings=Settings(),
        odoo_client=FakeJson2Client(),
        request_mapping=_parent_mapping(),
        line_request_mapping=_mapping(),
    )

    off = _tick_workflow(**common)
    on = _tick_workflow(create_request_mapping=_create_mapping(), **common)

    assert [c.name for c in off._channels] == ["product_line", "parent"]
    assert [c.name for c in on._channels] == ["product_line", "product_line_create", "parent"]
    create = on._channels[1].workflow
    assert set(create._handlers) == {OperatorRequestAction.PRODUCT_LINE_CREATE}
    # the live PR C mapping channel reads exactly the same fields as before PR D
    assert _mapping().read_fields() == [
        "id",
        "x_studio_ipp_workbench_id",
        "x_studio_ipp_line_key",
        "x_studio_ipp_is_current",
        "x_studio_ipp_review_id",
        "x_studio_ipp_company_id",
        "x_studio_ipp_line_number",
        "x_studio_ipp_req_product",
        "x_studio_ipp_req_ready",
        "x_studio_ipp_req_version",
        "x_studio_ipp_req_requested_by",
        "x_studio_ipp_req_requested_at",
    ]
    assert off._channels[0].workflow._reader._mapping.read_fields() == _mapping().read_fields()


def test_tick_builder_reads_create_fields_only_with_the_gate(monkeypatch) -> None:
    from app.composition import operator_requests

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        operator_requests,
        "OdooOperatorRequestFieldMapping",
        type("M", (), {"from_environment": staticmethod(lambda: _parent_mapping())}),
    )
    monkeypatch.setattr(operator_requests, "_tick_workflow", lambda **kwargs: captured.update(kwargs))
    engine = create_engine("sqlite://")
    for enabled in (False, True):
        tick = operator_requests.build_operator_request_tick(
            settings=_ready_settings(odoo_workbench_product_line_create_enabled=enabled), engine=engine
        )
        tick._workflow_factory(None, None)
        assert (captured["create_request_mapping"] is not None) is enabled


# --------------------------------------------------------------------------- Odoo read adapters


class _Adapter:
    def __init__(self, responses: list[tuple[dict[str, Any], ...]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def search_read(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def test_catalog_reader_compares_literal_text_across_archived_and_shared_products() -> None:
    adapter = _Adapter([({"id": 9},), ({"id": 4}, {"id": 2})])
    reader = OdooProductCatalogReader(adapter=adapter)

    assert reader.find_variant_ids_by_default_code(" ME_100% ", company_id=1) == (9,)
    assert reader.find_template_ids_by_name("Name_x", company_id=1) == (2, 4)

    variant_call, template_call = adapter.calls
    assert variant_call["model"] == "product.product"
    assert variant_call["domain"] == [
        ["default_code", "=ilike", "ME\\_100\\%"],
        ["active", "in", [True, False]],
        ["company_id", "in", [1, False]],
    ]
    assert template_call["domain"] == [["name", "=ilike", "Name\\_x"], ["company_id", "in", [1, False]]]
    assert escape_like("a\\b") == "a\\\\b"


def test_created_product_reader_snapshot() -> None:
    adapter = _Adapter(
        [
            (
                {
                    "id": 900,
                    "name": NAME,
                    "default_code": False,
                    "type": "service",
                    "uom_id": [1, "Units"],
                    "categ_id": [60, "x"],
                    "is_storable": False,
                },
            ),
            ({"id": 9900},),
        ]
    )
    snapshot = OdooCreatedProductReader(adapter=adapter).read_template(900)
    assert (snapshot.default_code, snapshot.uom_id, snapshot.categ_id, snapshot.variant_ids) == (None, 1, 60, (9900,))
    assert OdooCreatedProductReader(adapter=_Adapter([()])).read_template(901) is None


# --------------------------------------------------------------------------- migration 202607170040


@pytest.fixture
def migrated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    url = f"sqlite:///{tmp_path / 'pr-d.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        yield Config("alembic.ini"), create_engine(url)
    finally:
        get_settings.cache_clear()


def _insert_creation(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workbench_product_line_creations (review_id, company_id, review_version, line_number, "
                "status, supplier_partner_id, seller_item_code, product_name, product_kind, categ_id, uom_id, "
                "intent_digest, approved_by, create_attempted_at) VALUES ('review:x', 1, 2, '1', 'create_attempted', "
                "24, 'TFZP', 'n', 'service', 60, 1, 'd', 'a', '2026-10-12 09:00:00')"
            )
        )


def test_migration_creates_the_table_and_downgrade_refuses_while_rows_exist(migrated) -> None:
    config, engine = migrated
    command.upgrade(config, "head")
    assert "workbench_product_line_creations" in inspect(engine).get_table_names()
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO workbench_product_line_creations (review_id, company_id, review_version, line_number, "
                    "status, supplier_partner_id, seller_item_code, product_name, product_kind, categ_id, uom_id, "
                    "intent_digest, approved_by, create_attempted_at) VALUES ('review:x', 1, 2, '1', 'completed', "
                    "24, 'TFZP', 'n', 'service', 60, 1, 'd', 'a', '2026-10-12 09:00:00')"
                )
            )
    _insert_creation(engine)

    with pytest.raises(RuntimeError, match="Refusing to downgrade 202607170040"):
        command.downgrade(config, "202607170039")

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "202607170040"
        assert connection.scalar(text("SELECT count(*) FROM workbench_product_line_creations")) == 1


def test_migration_round_trip_without_rows(migrated) -> None:
    config, engine = migrated
    command.upgrade(config, "head")
    command.downgrade(config, "202607170039")
    assert "workbench_product_line_creations" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    _insert_creation(engine)


def test_migration_swaps_the_claim_owner_fk_for_an_owner_kind_and_restores_it(migrated) -> None:
    config, engine = migrated
    command.upgrade(config, "head")
    columns = {c["name"] for c in inspect(engine).get_columns("workbench_review_product_identity_claims")}
    assert "owner_kind" in columns
    assert inspect(engine).get_foreign_keys("workbench_review_product_identity_claims") == []
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workbench_review_product_identity_claims (company_id, resolved_supplier_partner_id, "
                "seller_item_code, owner_review_id, owner_company_id, owner_review_version, owner_line_number, "
                "owner_kind) VALUES (1, 24, 'TFZP', 'review:x', 1, 2, '1', 'product_line_creation')"
            )
        )
    with pytest.raises(RuntimeError, match="1 product_line_creation identity claim"):
        command.downgrade(config, "202607170039")
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM workbench_review_product_identity_claims"))

    command.downgrade(config, "202607170039")

    engine = create_engine(engine.url)  # reflect the rebuilt table through fresh connections
    (fk,) = inspect(engine).get_foreign_keys("workbench_review_product_identity_claims")
    assert fk["name"] == "fk_wrpr_identity_claims_owner"
    assert fk["referred_table"] == "workbench_review_product_remediation_reservations"
    columns = {c["name"] for c in inspect(engine).get_columns("workbench_review_product_identity_claims")}
    assert "owner_kind" not in columns


def test_uom_unit_code_map_is_bounded_by_the_allowlist() -> None:
    assert not any(
        "UOM_UNIT_CODES" in e
        for e in runtime_configuration_errors(_ready_settings(product_creation_uom_unit_codes={1: ("C62",)}))
    )
    errors = runtime_configuration_errors(_ready_settings(product_creation_uom_unit_codes={5: ("DAY",)}))
    assert any("may only list uom ids" in e for e in errors)
    errors = runtime_configuration_errors(_ready_settings(product_creation_uom_unit_codes={1: ()}))
    assert any("at least one unit code" in e for e in errors)


def test_vendor_code_lookup_spans_every_vendor_with_literal_text() -> None:
    adapter = _Adapter([({"id": 88},)])
    assert OdooProductCatalogReader(adapter=adapter).find_supplierinfo_ids_by_product_code(" XY_9 ", company_id=1) == (
        88,
    )
    (call,) = adapter.calls
    assert call["model"] == "product.supplierinfo"
    assert call["domain"] == [["product_code", "=ilike", "XY\\_9"], ["company_id", "in", [1, False]]]
