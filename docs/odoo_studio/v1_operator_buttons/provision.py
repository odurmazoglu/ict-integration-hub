r"""Validate (read-only, default), apply or roll back the ICT V1 Workbench operator buttons.

Runs inside the Hub API container, fed through stdin with the artifacts embedded:

    python3 docs/odoo_studio/v1_operator_buttons/provision.py --bundle \
      | ssh ict-hub-prod "docker exec -i -w /app config-api-1 sh -c \
        'ODOO_API_KEY=\"\$(cat /run/secrets/odoo_api_key)\" exec python - validate'"

Commands:
  validate  read-only: model, group, fields, selection values, existing objects, and every
            xpath of view.xml applied to the combined production form (get_views).
            Prints the plan digest.
  apply     creates the six server actions and the extension view, then re-reads the form;
            requires V1_CONFIRM=<digest printed by validate>. Separately approved step.
  rollback  deactivates the V1 view (active=False); the server actions stay inert.
            Requires V1_CONFIRM=rollback. Separately approved step.

Nothing here deletes a view, a field or server action 1026.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

MODEL = "x_ipp_import_workbench"
BASE_FORM_VIEW_ID = 3984
VIEW_PRIORITY = 260
VIEW_NAME = "ICT V1 Workbench operator buttons"
OPERATOR_GROUP_XMLID = ("studio_customization", "ipp_workbench_operator_group")
OPERATOR_GROUP_ID = 88
#: placeholder in view.xml -> (artifact file, server action name)
ACTIONS: dict[str, tuple[str, str]] = {
    "__SA_SUPPLIER__": ("supplier.py", "ICT V1 Tedarikçiyi Kaydet"),
    "__SA_PURPOSE__": ("purpose.py", "ICT V1 Satın Alma Amacını Kaydet"),
    "__SA_ACCOUNTING__": ("accounting.py", "ICT V1 Muhasebeyi Kaydet"),
    "__SA_DECISION__": ("decision.py", "ICT V1 Kararı Gönder"),
    "__SA_EXECUTE__": ("execute.py", "ICT V1 Taslak Alış Faturası Oluştur"),
    "__SA_OPEN_BILL__": ("open_bill.py", "ICT V1 Taslak Faturayı Aç"),
}
#: selection field -> values the view / actions compare against (must exist in Odoo)
REQUIRED_SELECTIONS: dict[str, tuple[str, ...]] = {
    "x_studio_ipp_next_action": (
        "Tedarikçi Doğrulanmalı",
        "Satın Alma Amacı Seçilmeli",
        "Muhasebe İşlemi Seçilmeli",
        "Karar Verilmeli",
        "Fatura Oluşturulmalı",
        "Tamamlandı",
    ),
    "x_studio_ipp_req_action": ("Tedarikçi Çözümü", "Satın Alma Amacı", "Muhasebe İşlemi", "Karar", "Fatura Oluştur"),
    "x_studio_ipp_req_supplier_mode": ("Mevcut Tedarikçiyi Seç",),
    "x_studio_ipp_req_treatment": ("Gider Hesabı", "Sabit Kıymet / Demirbaş"),
    "x_studio_decision": ("Submit Decision", "Dismiss"),
    "x_studio_review_status": ("Decision Submitted",),
}

ARTIFACTS: dict[str, str] | None = None  # embedded by --bundle


def load_artifacts() -> dict[str, str]:
    if ARTIFACTS is not None:
        return ARTIFACTS
    root = Path(__file__).resolve().parent
    artifacts = {"view.xml": (root / "view.xml").read_text(encoding="utf-8")}
    for file_name, _ in ACTIONS.values():
        artifacts[file_name] = (root / "actions" / file_name).read_text(encoding="utf-8")
    return artifacts


def plan_digest(artifacts: dict[str, str]) -> str:
    payload = json.dumps(artifacts, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


# ------------------------------------------------------------------ view inheritance (local)


def apply_extension(arch: str, extension: str) -> Any:
    """Apply an Odoo ``<data><xpath …/></data>`` extension to an arch, like Odoo does for
    the positions this view uses. Raises ``ValueError`` naming any xpath that matches nothing."""

    from lxml import etree

    tree = etree.fromstring(arch.encode("utf-8"))
    spec = etree.fromstring(extension.encode("utf-8"))
    for node in spec:
        if not isinstance(node.tag, str):
            continue  # comments
        if node.tag != "xpath":
            raise ValueError(f"Unsupported extension node <{node.tag}>.")
        expr, position = node.get("expr"), node.get("position", "inside")
        if "@string" in expr:
            raise ValueError(f"xpath must not anchor on @string (Odoo 19 rejects it): {expr}")
        matches = tree.xpath(expr)
        if not matches:
            raise ValueError(f"xpath matched nothing: {expr}")
        target = matches[0]
        children = [copy.deepcopy(child) for child in node if isinstance(child.tag, str)]
        if position == "attributes":
            for attribute in node:
                if isinstance(attribute.tag, str):
                    target.set(attribute.get("name"), attribute.text or "")
        elif position == "inside":
            target.extend(children)
        elif position == "replace":
            parent = target.getparent()
            index = parent.index(target)
            parent.remove(target)
            for offset, child in enumerate(children):
                parent.insert(index + offset, child)
        elif position in ("after", "before"):
            parent = target.getparent()
            index = parent.index(target) + (1 if position == "after" else 0)
            for offset, child in enumerate(children):
                parent.insert(index + offset, child)
        else:
            raise ValueError(f"Unsupported position {position!r}.")
    return tree


def view_arch(artifacts: dict[str, str], action_ids: dict[str, int]) -> str:
    arch = artifacts["view.xml"]
    for placeholder, action_id in action_ids.items():
        arch = arch.replace(placeholder, str(int(action_id)))
    if "__SA_" in arch:
        raise ValueError("Unreplaced server action placeholder in view.xml.")
    return arch


def referenced_fields(extension: str) -> set[str]:
    import re

    names = set(re.findall(r"@name='(x_[a-z0-9_]+)'", extension))
    names |= set(re.findall(r'name="(x_[a-z0-9_]+)"', extension))
    names |= set(re.findall(r"\b(x_studio_[a-z0-9_]+)\b", extension))
    return names


def action_fields(code: str) -> set[str]:
    import re

    return set(re.findall(r"\b(x_studio_[a-z0-9_]+)\b", code))


# ------------------------------------------------------------------ Odoo access


class Odoo:
    def __init__(self) -> None:
        import httpx

        from app.core.config import get_settings

        settings = get_settings()
        self._client = httpx.Client(
            base_url=str(settings.odoo_base_url).rstrip("/"),
            timeout=60,
            headers={
                "Authorization": f"bearer {settings.odoo_api_key.get_secret_value()}",
                "X-Odoo-Database": settings.odoo_database,
                "Content-Type": "application/json",
            },
        )

    def call(self, model: str, method: str, payload: dict[str, Any]) -> Any:
        response = self._client.post(f"/json/2/{model}/{method}", json=payload)
        if response.status_code != 200:
            raise RuntimeError(f"Odoo {model}.{method} failed with HTTP {response.status_code}.")
        return response.json()

    def search_read(self, model: str, domain: list[Any], fields: list[str], **extra: Any) -> list[dict[str, Any]]:
        return self.call(model, "search_read", {"domain": domain, "fields": fields, **extra})

    def combined_form(self) -> str:
        return self.call(MODEL, "get_views", {"views": [[False, "form"]], "options": {}})["views"]["form"]["arch"]


def validate(odoo: Odoo, artifacts: dict[str, str]) -> dict[str, Any]:
    problems: list[str] = []
    models = odoo.search_read("ir.model", [["model", "=", MODEL]], ["id"])
    model_id = models[0]["id"] if len(models) == 1 else None
    if model_id is None:
        problems.append(f"model {MODEL} not found")
    group = odoo.search_read(
        "ir.model.data",
        [["module", "=", OPERATOR_GROUP_XMLID[0]], ["name", "=", OPERATOR_GROUP_XMLID[1]]],
        ["res_id", "model"],
    )
    if not group or group[0]["res_id"] != OPERATOR_GROUP_ID or group[0]["model"] != "res.groups":
        problems.append("operator group xml id does not resolve to res.groups 88")

    wanted = referenced_fields(artifacts["view.xml"])
    for file_name, _ in ACTIONS.values():
        wanted |= action_fields(artifacts[file_name])
    existing = {
        row["name"] for row in odoo.search_read("ir.model.fields", [["model", "=", MODEL]], ["name"], limit=1000)
    }
    problems += [f"missing field {name}" for name in sorted(wanted - existing)]

    for field_name, values in REQUIRED_SELECTIONS.items():
        rows = odoo.search_read(
            "ir.model.fields.selection",
            [["field_id.model", "=", MODEL], ["field_id.name", "=", field_name]],
            ["value"],
            limit=200,
        )
        present = {row["value"] for row in rows}
        problems += [f"missing selection {field_name}={value!r}" for value in values if value not in present]

    names = [name for _, name in ACTIONS.values()]
    existing_actions = odoo.search_read(
        "ir.actions.server", [["name", "in", names]], ["id", "name"], context={"active_test": False}
    )
    existing_views = odoo.search_read(
        "ir.ui.view", [["name", "=", VIEW_NAME]], ["id", "active"], context={"active_test": False}
    )
    base = odoo.search_read("ir.ui.view", [["id", "=", BASE_FORM_VIEW_ID]], ["model", "type", "mode", "active"])
    if not base or base[0]["model"] != MODEL or base[0]["type"] != "form" or not base[0]["active"]:
        problems.append("base form view 3984 is not the active primary form of the model")

    try:
        dummy_ids = {placeholder: 900000 + index for index, placeholder in enumerate(ACTIONS)}
        result = apply_extension(odoo.combined_form(), view_arch(artifacts, dummy_ids))
        buttons = result.xpath("//header/button")
        visible_dropdown = [
            node for node in result.xpath("//field[@name='x_studio_ipp_req_action']") if node.get("invisible") != "1"
        ]
        if visible_dropdown:
            problems.append("İşlem dropdown would remain visible")
    except ValueError as exc:
        problems.append(str(exc))
        buttons = []
    return {
        "digest": plan_digest(artifacts),
        "model_id": model_id,
        "existing_actions": existing_actions,
        "existing_views": existing_views,
        "header_buttons_after": [node.get("string") for node in buttons],
        "problems": problems,
    }


def apply(odoo: Odoo, artifacts: dict[str, str], report: dict[str, Any]) -> None:
    if report["problems"]:
        raise SystemExit("Validation failed; nothing written.")
    if report["existing_actions"] or report["existing_views"]:
        raise SystemExit("V1 objects already exist; nothing written (inspect them first).")
    action_ids: dict[str, int] = {}
    for placeholder, (file_name, name) in ACTIONS.items():
        created = odoo.call(
            "ir.actions.server",
            "create",
            {
                "vals_list": [
                    {
                        "name": name,
                        "model_id": report["model_id"],
                        "state": "code",
                        "code": artifacts[file_name],
                        "group_ids": [[6, 0, [OPERATOR_GROUP_ID]]],
                    }
                ]
            },
        )
        action_ids[placeholder] = created[0] if isinstance(created, list) else created
        print("created server action", name, action_ids[placeholder])
    created_view = odoo.call(
        "ir.ui.view",
        "create",
        {
            "vals_list": [
                {
                    "name": VIEW_NAME,
                    "model": MODEL,
                    "type": "form",
                    "mode": "extension",
                    "inherit_id": BASE_FORM_VIEW_ID,
                    "priority": VIEW_PRIORITY,
                    "arch_db": view_arch(artifacts, action_ids),
                }
            ]
        },
    )
    view_id = created_view[0] if isinstance(created_view, list) else created_view
    print("created view", view_id)
    combined = odoo.combined_form()
    if any(f'name="{action_id}"' not in combined for action_id in action_ids.values()):
        odoo.call("ir.ui.view", "write", {"ids": [view_id], "vals": {"active": False}})
        raise SystemExit(f"Post-check failed; view {view_id} deactivated (rollback).")
    print("post-check ok")


def rollback(odoo: Odoo) -> None:
    views = odoo.search_read("ir.ui.view", [["name", "=", VIEW_NAME]], ["id", "active"], context={"active_test": False})
    ids = [view["id"] for view in views if view["active"]]
    if ids:
        odoo.call("ir.ui.view", "write", {"ids": ids, "vals": {"active": False}})
    print("deactivated views", ids)


def bundle() -> str:
    source = Path(__file__).read_text(encoding="utf-8")
    embedded = json.dumps(load_artifacts(), ensure_ascii=False)
    return source.replace(
        "ARTIFACTS: dict[str, str] | None = None  # embedded by --bundle",
        f"ARTIFACTS: dict[str, str] | None = json.loads({embedded!r})",
        1,
    )


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "validate"
    if command == "--bundle":
        sys.stdout.write(bundle())
        return 0
    artifacts = load_artifacts()
    odoo = Odoo()
    if command == "rollback":
        if os.environ.get("V1_CONFIRM") != "rollback":
            raise SystemExit("rollback requires V1_CONFIRM=rollback")
        rollback(odoo)
        return 0
    report = validate(odoo, artifacts)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if command == "validate":
        return 1 if report["problems"] else 0
    if command == "apply":
        if os.environ.get("V1_CONFIRM") != report["digest"]:
            raise SystemExit("apply requires V1_CONFIRM=<digest printed by validate>")
        apply(odoo, artifacts, report)
        return 0
    raise SystemExit(f"unknown command {command!r}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
