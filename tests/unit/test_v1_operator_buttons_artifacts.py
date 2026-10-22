"""V1 Workbench operator buttons: Studio view + server action artifacts.

The artifacts (docs/odoo_studio/v1_operator_buttons) are applied to Odoo only in a
separately approved step. These tests pin them to:

* the combined production form captured on 2026-10-21 (every xpath must resolve);
* the Hub's own label mappings (a button can only queue an action the Hub understands);
* server-side validation, double-submission refusal and stale-step refusal in each
  server action, exercised through a fake Odoo record;
* Odoo ``safe_eval`` compatible syntax, and no posting / no account.move write.
"""

from __future__ import annotations

import ast
import datetime
import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.application.workbench.operator_guidance import OperatorNextAction
from app.application.workbench.operator_request_ingestion import OperatorRequestAction
from app.erp.odoo.workbench_candidate_reader import (
    ODOO_DECISION_TO_CANONICAL,
    ODOO_SELECTED_WORKFLOW_TO_CANONICAL,
)
from app.erp.odoo.workbench_operator_request_reader import (
    ACTION_BY_ODOO_VALUE,
    SUPPLIER_MODE_BY_ODOO_VALUE,
    TREATMENT_BY_ODOO_VALUE,
)
from app.erp.odoo.workbench_projection_publisher import ODOO_REVIEW_STATUS_BY_CANONICAL

ROOT = Path(__file__).resolve().parents[2] / "docs" / "odoo_studio" / "v1_operator_buttons"
SNAPSHOT = ROOT / "production_form_combined_20261021.xml"


def _provision():
    spec = importlib.util.spec_from_file_location("v1_provision", ROOT / "provision.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


PROVISION = _provision()
ARTIFACTS = PROVISION.load_artifacts()
DUMMY_IDS = {placeholder: 900000 + index for index, placeholder in enumerate(PROVISION.ACTIONS)}


def _applied():
    return PROVISION.apply_extension(SNAPSHOT.read_text(encoding="utf-8"), PROVISION.view_arch(ARTIFACTS, DUMMY_IDS))


def _code(placeholder: str) -> str:
    return ARTIFACTS[PROVISION.ACTIONS[placeholder][0]]


# =========================================================================== view


def test_view_applies_to_the_production_form_and_hides_the_generic_dropdown() -> None:
    form = _applied()

    generic = form.xpath("//header/button[@name='1026']")
    assert len(generic) == 1 and generic[0].get("invisible") == "1"  # kept, only hidden
    assert [node.get("invisible") for node in form.xpath("//field[@name='x_studio_ipp_req_action']")] == ["1"]
    assert form.xpath("//field[@name='x_studio_ipp_req_action']/../..")[0].get("string") == "Bilgileri Girin"
    # Only the two dropdown-era hints still mention it, and both are hidden.
    stale = form.xpath("//*[contains(text(), 'İşleme Gönder')]")
    assert {node.get("name") for node in stale} == {"ipp_golive_resale_hint", "ipp_internal_use_purpose_hint"}
    assert all(node.get("invisible") == "1" for node in stale)
    for hint in ("ipp_golive_resale_hint", "ipp_internal_use_purpose_hint"):
        assert form.xpath(f"//div[@name='{hint}']")[0].get("invisible") == "1"


EXPECTED_BUTTONS = {
    "Tedarikçiyi Kaydet": ("__SA_SUPPLIER__", "Tedarikçi Doğrulanmalı"),
    "Muhasebeyi Kaydet": ("__SA_ACCOUNTING__", "Muhasebe İşlemi Seçilmeli"),
    "Kararı Gönder": ("__SA_DECISION__", "Karar Verilmeli"),
    "Taslak Alış Faturası Oluştur": ("__SA_EXECUTE__", "Fatura Oluşturulmalı"),
}


def test_one_context_sensitive_button_per_step() -> None:
    buttons = {node.get("string"): node for node in _applied().xpath("//header/button") if node.get("name") != "1026"}
    for label, (placeholder, stage) in EXPECTED_BUTTONS.items():
        node = buttons[label]
        assert node.get("name") == str(DUMMY_IDS[placeholder])
        assert node.get("type") == "action"
        assert node.get("groups") == "studio_customization.ipp_workbench_operator_group"
        invisible = node.get("invisible")
        assert invisible.startswith("x_studio_ipp_req_ready or ")  # never while a request is queued
        assert f"x_studio_ipp_next_action != '{stage}'" in invisible
    purpose = [
        node for node in _applied().xpath("//header/button") if node.get("string") == "Satın Alma Amacını Kaydet"
    ]
    assert len(purpose) == 2
    assert "'Satın Alma Amacı Seçilmeli'" in purpose[0].get("invisible")
    assert "not x_studio_ipp_req_purpose" in purpose[1].get("invisible")
    assert "x_studio_vendor_bill" in buttons["Taslak Alış Faturası Oluştur"].get("invisible")
    assert buttons["Taslak Faturayı Aç"].get("invisible") == "not x_studio_vendor_bill"


def test_irreversible_steps_ask_for_confirmation_and_stay_separate() -> None:
    buttons = {node.get("string"): node for node in _applied().xpath("//header/button")}
    assert "geri alınamaz" in buttons["Kararı Gönder"].get("confirm")
    assert "TASLAK" in buttons["Taslak Alış Faturası Oluştur"].get("confirm")
    assert "onaylanmaz" in buttons["Taslak Alış Faturası Oluştur"].get("confirm")
    # Decision and execution are two different server actions behind two different steps.
    assert buttons["Kararı Gönder"].get("name") != buttons["Taslak Alış Faturası Oluştur"].get("name")


def test_draft_warning_is_the_approved_text() -> None:
    warning = _applied().xpath("//div[@name='ipp_golive_no_post']")[0]
    assert warning.text == (
        "Taslak alış faturası oluşturuldu. Faturayı Odoo'da kontrol edin. Muhasebe geçiş kararı olmadan "
        "ONAYLAMAYIN; ödeme veya mutabakat yapmayın."
    )


def test_view_never_anchors_on_string_and_keeps_technical_tab() -> None:
    assert "@string" not in ARTIFACTS["view.xml"]
    # get_views resolves ``groups`` for the reading user; view 4206 keeps base.group_system.
    assert len(_applied().xpath("//page[@name='ipp_technical']")) == 1
    assert "ipp_technical" not in ARTIFACTS["view.xml"]


def test_unreplaced_placeholders_are_refused() -> None:
    with pytest.raises(ValueError, match="placeholder"):
        PROVISION.view_arch(ARTIFACTS, {})


def test_a_missing_anchor_is_reported() -> None:
    with pytest.raises(ValueError, match="matched nothing"):
        PROVISION.apply_extension(
            SNAPSHOT.read_text(encoding="utf-8"),
            '<data><xpath expr="//field[@name=\'x_missing\']" position="after"><div/></xpath></data>',
        )


# =========================================================================== labels = Hub mappings


def test_queued_actions_are_hub_actions() -> None:
    expected = {
        "__SA_SUPPLIER__": OperatorRequestAction.SUPPLIER_RESOLUTION,
        "__SA_PURPOSE__": OperatorRequestAction.PURCHASE_PURPOSE,
        "__SA_ACCOUNTING__": OperatorRequestAction.ACCOUNTING_RESOLUTION,
        "__SA_DECISION__": OperatorRequestAction.DECISION,
        "__SA_EXECUTE__": OperatorRequestAction.EXECUTE_VENDOR_BILL,
    }
    for placeholder, action in expected.items():
        labels = re.findall(r'"x_studio_ipp_req_action": "([^"]+)"', _code(placeholder))
        assert len(labels) == 1
        assert ACTION_BY_ODOO_VALUE[labels[0]] is action


def test_compared_labels_exist_in_hub_mappings() -> None:
    sources = ARTIFACTS["view.xml"] + "".join(_code(placeholder) for placeholder in PROVISION.ACTIONS)
    stages = set(re.findall(r"x_studio_ipp_next_action (?:!=|==) '([^']+)'", sources))
    stages |= set(re.findall(r'x_studio_ipp_next_action (?:!=|==) "([^"]+)"', sources))
    stages |= {"Satın Alma Amacı Seçilmeli", "Karar Verilmeli"}
    assert stages <= {action.value for action in OperatorNextAction}
    assert {"Submit Decision", "Dismiss"} <= set(ODOO_DECISION_TO_CANONICAL)
    assert "Mevcut Tedarikçiyi Seç" in SUPPLIER_MODE_BY_ODOO_VALUE
    assert {"Gider Hesabı", "Sabit Kıymet / Demirbaş"} <= set(TREATMENT_BY_ODOO_VALUE)
    assert "Decision Submitted" in ODOO_REVIEW_STATUS_BY_CANONICAL.values()
    assert "Direct Vendor Bill" in ODOO_SELECTED_WORKFLOW_TO_CANONICAL


def test_provision_requires_every_compared_selection_value() -> None:
    required = PROVISION.REQUIRED_SELECTIONS
    assert set(EXPECTED_BUTTONS[label][1] for label in EXPECTED_BUTTONS) <= set(required["x_studio_ipp_next_action"])
    assert set(required["x_studio_ipp_req_action"]) <= set(ACTION_BY_ODOO_VALUE)


# =========================================================================== server actions


class UserError(Exception):
    pass


class _Record(SimpleNamespace):
    def __init__(self, **values: Any) -> None:
        defaults: dict[str, Any] = {
            "x_studio_ipp_req_ready": False,
            "x_studio_ipp_next_action": False,
            "x_studio_review_version": 4,
            "x_studio_review_status": "Pending Review",
            "x_studio_vendor_bill": False,
            "x_studio_ipp_req_supplier_mode": False,
            "x_studio_ipp_req_partner": False,
            "x_studio_ipp_req_purpose": False,
            "x_studio_ipp_req_treatment": False,
            "x_studio_ipp_req_expense_account": False,
            "x_studio_ipp_req_expense_category_code": False,
            "x_studio_ipp_req_asset_account": False,
            "x_studio_ipp_req_depreciation_model": False,
            "x_studio_ipp_eligible_asset_account_ids": [],
            "x_studio_decision": False,
            "x_studio_selected_workflow": False,
            "x_studio_decision_comment": False,
        }
        defaults.update(values)
        super().__init__(**defaults)
        self.writes: list[dict[str, Any]] = []

    def __len__(self) -> int:
        return 1

    def write(self, values: dict[str, Any]) -> bool:
        self.writes.append(values)
        return True


class _Many(list):
    pass


def _run(placeholder: str, record: Any) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "records": record,
        "record": record,
        "env": SimpleNamespace(user=SimpleNamespace(id=2)),
        "datetime": datetime,
        "UserError": UserError,
    }
    exec(compile(_code(placeholder), placeholder, "exec"), scope)  # noqa: S102 - test harness for Odoo code
    return scope


def _queued(record: _Record, action: str) -> None:
    assert len(record.writes) == 1
    values = record.writes[0]
    assert values["x_studio_ipp_req_action"] == action
    assert values["x_studio_ipp_req_version"] == record.x_studio_review_version
    assert values["x_studio_ipp_req_requested_by"] == 2
    assert isinstance(values["x_studio_ipp_req_requested_at"], datetime.datetime)
    assert values["x_studio_ipp_req_ready"] is True
    assert values["x_studio_ipp_req_result"] is False and values["x_studio_ipp_req_message"] is False
    assert set(values) == {
        "x_studio_ipp_req_action",
        "x_studio_ipp_req_version",
        "x_studio_ipp_req_requested_by",
        "x_studio_ipp_req_requested_at",
        "x_studio_ipp_req_result",
        "x_studio_ipp_req_message",
        "x_studio_ipp_req_ready",
    }


def _refused(placeholder: str, record: _Record, fragment: str) -> None:
    with pytest.raises(UserError, match=fragment):
        _run(placeholder, record)
    assert record.writes == []


SUPPLIER = "Tedarikçi Doğrulanmalı"


def test_supplier_validation_and_queue() -> None:
    _refused("__SA_SUPPLIER__", _Record(x_studio_ipp_next_action=SUPPLIER), "Tedarikçi İşlemi")
    _refused(
        "__SA_SUPPLIER__",
        _Record(x_studio_ipp_next_action=SUPPLIER, x_studio_ipp_req_supplier_mode="Mevcut Tedarikçiyi Seç"),
        "Mevcut Tedarikçi",
    )
    record = _Record(
        x_studio_ipp_next_action=SUPPLIER,
        x_studio_ipp_req_supplier_mode="Mevcut Tedarikçiyi Seç",
        x_studio_ipp_req_partner=SimpleNamespace(id=451),
    )
    _run("__SA_SUPPLIER__", record)
    _queued(record, "Tedarikçi Çözümü")
    created = _Record(x_studio_ipp_next_action=SUPPLIER, x_studio_ipp_req_supplier_mode="Kalıcı Tedarikçi Oluştur")
    _run("__SA_SUPPLIER__", created)
    _queued(created, "Tedarikçi Çözümü")


@pytest.mark.parametrize("stage", ["Satın Alma Amacı Seçilmeli", "Karar Verilmeli"])
def test_purpose_validation_and_queue(stage: str) -> None:
    _refused("__SA_PURPOSE__", _Record(x_studio_ipp_next_action=stage), "Satın Alma Amacı")
    record = _Record(x_studio_ipp_next_action=stage, x_studio_ipp_req_purpose="Yeniden Satış")
    _run("__SA_PURPOSE__", record)
    _queued(record, "Satın Alma Amacı")


ACCOUNTING = "Muhasebe İşlemi Seçilmeli"


def test_accounting_expense_validation_and_queue() -> None:
    _refused("__SA_ACCOUNTING__", _Record(x_studio_ipp_next_action=ACCOUNTING), "Muhasebe İşlemi")
    _refused(
        "__SA_ACCOUNTING__",
        _Record(x_studio_ipp_next_action=ACCOUNTING, x_studio_ipp_req_treatment="Gider Hesabı"),
        "Gider Hesabı, Gider Kategorisi",
    )
    record = _Record(
        x_studio_ipp_next_action=ACCOUNTING,
        x_studio_ipp_req_treatment="Gider Hesabı",
        x_studio_ipp_req_expense_account=SimpleNamespace(id=247),
        x_studio_ipp_req_expense_category_code="SOFTWARE_SUBSCRIPTION_INTERNAL",
    )
    _run("__SA_ACCOUNTING__", record)
    _queued(record, "Muhasebe İşlemi")


def test_accounting_fixed_asset_requires_an_eligible_account() -> None:
    eligible = SimpleNamespace(id=74)
    _refused(
        "__SA_ACCOUNTING__",
        _Record(x_studio_ipp_next_action=ACCOUNTING, x_studio_ipp_req_treatment="Sabit Kıymet / Demirbaş"),
        "Demirbaş Hesabı, Amortisman Modeli",
    )
    _refused(
        "__SA_ACCOUNTING__",
        _Record(
            x_studio_ipp_next_action=ACCOUNTING,
            x_studio_ipp_req_treatment="Sabit Kıymet / Demirbaş",
            x_studio_ipp_req_asset_account=SimpleNamespace(id=99),
            x_studio_ipp_req_depreciation_model=SimpleNamespace(id=5),
            x_studio_ipp_eligible_asset_account_ids=_Many([eligible]),
        ),
        "izin verilen",
    )
    record = _Record(
        x_studio_ipp_next_action=ACCOUNTING,
        x_studio_ipp_req_treatment="Sabit Kıymet / Demirbaş",
        x_studio_ipp_req_asset_account=eligible,
        x_studio_ipp_req_depreciation_model=SimpleNamespace(id=5),
        x_studio_ipp_eligible_asset_account_ids=_Many([eligible]),
    )
    _run("__SA_ACCOUNTING__", record)
    _queued(record, "Muhasebe İşlemi")


DECISION = "Karar Verilmeli"


def test_decision_validation_and_queue() -> None:
    _refused("__SA_DECISION__", _Record(x_studio_ipp_next_action=DECISION), "Karar")
    _refused(
        "__SA_DECISION__",
        _Record(x_studio_ipp_next_action=DECISION, x_studio_decision="Submit Decision"),
        "İş Akışı",
    )
    _refused(
        "__SA_DECISION__",
        _Record(x_studio_ipp_next_action=DECISION, x_studio_decision="Dismiss", x_studio_decision_comment="  "),
        "gerekçesi",
    )
    record = _Record(
        x_studio_ipp_next_action=DECISION,
        x_studio_decision="Submit Decision",
        x_studio_selected_workflow="Direct Vendor Bill",
    )
    _run("__SA_DECISION__", record)
    _queued(record, "Karar")


EXECUTE = "Fatura Oluşturulmalı"


def test_execute_requires_an_accepted_decision_and_no_existing_bill() -> None:
    _refused(
        "__SA_EXECUTE__",
        _Record(x_studio_ipp_next_action=EXECUTE, x_studio_review_status="Pending Review"),
        "karar kabul",
    )
    _refused(
        "__SA_EXECUTE__",
        _Record(
            x_studio_ipp_next_action=EXECUTE,
            x_studio_review_status="Decision Submitted",
            x_studio_vendor_bill=SimpleNamespace(id=76),
        ),
        "zaten oluşturuldu",
    )
    record = _Record(x_studio_ipp_next_action=EXECUTE, x_studio_review_status="Decision Submitted")
    _run("__SA_EXECUTE__", record)
    _queued(record, "Fatura Oluştur")


QUEUEING = {
    "__SA_SUPPLIER__": SUPPLIER,
    "__SA_PURPOSE__": "Satın Alma Amacı Seçilmeli",
    "__SA_ACCOUNTING__": ACCOUNTING,
    "__SA_DECISION__": DECISION,
    "__SA_EXECUTE__": EXECUTE,
}


@pytest.mark.parametrize("placeholder", sorted(QUEUEING))
def test_double_submission_is_refused(placeholder: str) -> None:
    _refused(
        placeholder,
        _Record(x_studio_ipp_next_action=QUEUEING[placeholder], x_studio_ipp_req_ready=True),
        "gönderilmiş bir istek var",
    )


@pytest.mark.parametrize("placeholder", sorted(QUEUEING))
def test_stale_step_and_unsynced_rows_are_refused(placeholder: str) -> None:
    _refused(placeholder, _Record(x_studio_ipp_next_action="Tamamlandı"), "Sayfayı yenileyin")
    _refused(placeholder, _Record(x_studio_ipp_next_action=False), "Sayfayı yenileyin")


def test_unsynced_row_is_refused() -> None:
    record = _Record(x_studio_ipp_next_action=SUPPLIER, x_studio_review_version=0)
    _refused("__SA_SUPPLIER__", record, "eşitlenmedi")


@pytest.mark.parametrize("placeholder", sorted(PROVISION.ACTIONS))
def test_multi_record_calls_are_refused(placeholder: str) -> None:
    class _Two(_Record):
        def __len__(self) -> int:
            return 2

    _refused(placeholder, _Two(x_studio_ipp_next_action=QUEUEING.get(placeholder, False)), "tek bir kayıt")


def test_open_bill_only_returns_a_window_action() -> None:
    with pytest.raises(UserError, match="taslak fatura yok"):
        _run("__SA_OPEN_BILL__", _Record())
    record = _Record(x_studio_vendor_bill=SimpleNamespace(id=76))
    scope = _run("__SA_OPEN_BILL__", record)
    assert scope["action"] == {
        "type": "ir.actions.act_window",
        "res_model": "account.move",
        "res_id": 76,
        "views": [[False, "form"]],
        "target": "current",
    }
    assert record.writes == []


# =========================================================================== Odoo safety

EVAL_CONTEXT = {"records", "record", "env", "datetime", "UserError", "len"}


@pytest.mark.parametrize("placeholder", sorted(PROVISION.ACTIONS))
def test_server_action_code_is_safe_eval_compatible(placeholder: str) -> None:
    tree = ast.parse(_code(placeholder))
    assigned = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    for node in ast.walk(tree):
        assert not isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Lambda, ast.ClassDef))
        assert not isinstance(node, (ast.Global, ast.Nonlocal, ast.With, ast.Try))
        if isinstance(node, ast.Attribute):
            assert not node.attr.startswith("_")
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            assert node.id in EVAL_CONTEXT | assigned | {"False", "True"}, node.id


@pytest.mark.parametrize("placeholder", sorted(PROVISION.ACTIONS))
def test_server_actions_never_post_or_write_bills(placeholder: str) -> None:
    code = _code(placeholder)
    for forbidden in ("action_post", "unlink", "env[", "sudo", "account.payment", "button_validate"):
        assert forbidden not in code
    writes = re.findall(r"\.write\(", code)
    assert len(writes) == (0 if placeholder == "__SA_OPEN_BILL__" else 1)


def test_bundle_embeds_all_artifacts_and_compiles() -> None:
    source = PROVISION.bundle()
    compile(source, "bundle", "exec")
    assert "json.loads(" in source
    for file_name, _ in PROVISION.ACTIONS.values():
        assert file_name in source


def test_plan_digest_changes_with_any_artifact() -> None:
    changed = dict(ARTIFACTS, **{"view.xml": ARTIFACTS["view.xml"] + " "})
    assert PROVISION.plan_digest(changed) != PROVISION.plan_digest(ARTIFACTS)
