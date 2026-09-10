"""Controlled Odoo supplier partner writer (P0-3D2C).

A narrow, gated, VAT-idempotent capability to create exactly one ``res.partner``
from immutable supplier identity. ``PartnerMatchingEngine`` stays read-only; this
writer is the only sanctioned ``res.partner`` create path and is impossible to
reach with default settings.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from app.application.commands import CreateSupplierPartnerCommand
from app.application.dto import SupplierPartnerWriteStatus
from app.application.exceptions.supplier_partner import (
    SupplierPartnerAmbiguityError,
    SupplierPartnerDataIntegrityError,
    SupplierPartnerDuplicateRaceError,
    SupplierPartnerInactiveError,
    SupplierPartnerWriteSafetyGateError,
    SupplierPartnerWriteValidationError,
)
from app.connectors.exceptions import ConnectorAuthenticationError
from app.core.config import Settings
from app.core.runtime_checks import PRODUCTION_APPROVAL_ACK
from app.erp.write import (
    OdooSupplierPartnerRepository,
    OdooSupplierPartnerWritePolicy,
    OdooSupplierPartnerWriter,
)

COMPANY_ID = 1
VKN = "0430367181"
NAME = "Akyaşam Yönetim Hizmetleri A.Ş."
STAGING_HOST_URL = "https://test-ictteknoloji.odoo.com"
SECRET_MARKER = "sk-super-secret-odoo-key"


class FakeJson2Client:
    def __init__(
        self,
        *,
        create_result: Any = 501,
        search_results: list[Any] | None = None,
        search_sequence: list[Any] | None = None,
    ) -> None:
        self.create_result = create_result
        self.search_results = search_results
        self.search_sequence = list(search_sequence) if search_sequence is not None else None
        self.create_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []

    async def create_res_partner(self, payload: dict[str, Any]) -> int:
        self.create_calls.append(payload)
        if isinstance(self.create_result, Exception):
            raise self.create_result
        return self.create_result

    async def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self.search_calls.append({"model": model, "domain": domain, "fields": fields, "limit": limit})
        if self.search_sequence is not None:
            result = self.search_sequence.pop(0)
        else:
            result = self.search_results if self.search_results is not None else []
        if isinstance(result, Exception):
            raise result
        return result


def _partner_row(
    *,
    partner_id: int = 501,
    name: str = NAME,
    vat: str = VKN,
    active: bool = True,
    company_id: Any = False,
) -> dict[str, Any]:
    return {"id": partner_id, "name": name, "vat": vat, "active": active, "company_id": company_id}


def _command(**overrides: Any) -> CreateSupplierPartnerCommand:
    kwargs: dict[str, Any] = {
        "company_id": COMPANY_ID,
        "supplier_name": NAME,
        "supplier_tax_number": VKN,
        "idempotency_key": "supplier-remediation:1:0430367181",
        "approved_by": "finance.operator",
    }
    kwargs.update(overrides)
    return CreateSupplierPartnerCommand(**kwargs)


def _enabled_policy() -> OdooSupplierPartnerWritePolicy:
    return OdooSupplierPartnerWritePolicy(
        supplier_remediation_write_enabled=True,
        app_env="staging",
        odoo_host="test-ictteknoloji.odoo.com",
    )


def _writer(
    client: FakeJson2Client, *, policy: OdooSupplierPartnerWritePolicy | None = None
) -> OdooSupplierPartnerWriter:
    return OdooSupplierPartnerWriter(
        repository=OdooSupplierPartnerRepository(client=client),
        policy=policy if policy is not None else _enabled_policy(),
    )


# --------------------------------------------------------- Phase 24: CREATED


async def test_creates_supplier_partner_when_no_exact_vat_match(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeJson2Client(create_result=777, search_sequence=[[], [_partner_row(partner_id=777)]])
    result = await _writer(client).create_supplier(_command())

    assert result.status is SupplierPartnerWriteStatus.CREATED
    assert result.partner_id == 777
    assert result.company_id == COMPANY_ID
    assert result.supplier_tax_number == VKN
    assert len(client.create_calls) == 1
    assert client.create_calls[0] == {"name": NAME, "vat": VKN, "company_type": "company"}
    # exact-VAT search before create, then the post-create re-query/read-back.
    assert len(client.search_calls) == 2
    assert client.search_calls[0]["domain"] == [
        ["vat", "=", VKN],
        ["company_id", "in", [COMPANY_ID, False]],
        ["active", "in", [True, False]],
    ]


# --------------------------------------------------------- Phase 25: ALREADY_EXISTS


async def test_returns_already_exists_for_single_exact_vat_match() -> None:
    client = FakeJson2Client(search_results=[_partner_row(partner_id=42)])
    result = await _writer(client).create_supplier(_command())

    assert result.status is SupplierPartnerWriteStatus.ALREADY_EXISTS
    assert result.partner_id == 42
    assert result.existing_by == "vat"
    assert result.name_mismatch is False
    assert client.create_calls == []


# --------------------------------------------------------- Phase 26: AMBIGUOUS


async def test_multiple_exact_vat_matches_fail_closed() -> None:
    client = FakeJson2Client(search_results=[_partner_row(partner_id=1), _partner_row(partner_id=2)])
    with pytest.raises(SupplierPartnerAmbiguityError):
        await _writer(client).create_supplier(_command())
    assert client.create_calls == []


# --------------------------------------------------------- Phase 27: INACTIVE


async def test_archived_exact_vat_partner_fails_closed_without_reactivation() -> None:
    client = FakeJson2Client(search_results=[_partner_row(partner_id=9, active=False)])
    with pytest.raises(SupplierPartnerInactiveError):
        await _writer(client).create_supplier(_command())
    # No create and no write of any kind: no reactivation, no duplicate.
    assert client.create_calls == []


# --------------------------------------------------------- Phase 28: NAME MISMATCH


async def test_name_mismatch_on_existing_vat_does_not_rename_or_create() -> None:
    client = FakeJson2Client(search_results=[_partner_row(partner_id=55, name="AKYASAM YONETIM HIZMETLERI ANONIM")])
    result = await _writer(client).create_supplier(_command())

    assert result.status is SupplierPartnerWriteStatus.ALREADY_EXISTS
    assert result.partner_id == 55
    assert result.name_mismatch is True
    assert any("differs" in warning for warning in result.warnings)
    assert client.create_calls == []


# --------------------------------------------------------- Phase 29 / 15: GATE OFF


async def test_default_settings_cannot_create_supplier() -> None:
    default_policy = OdooSupplierPartnerWritePolicy.from_settings(Settings())
    assert default_policy.supplier_remediation_write_enabled is False
    client = FakeJson2Client()
    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        await _writer(client, policy=default_policy).create_supplier(_command())
    assert client.create_calls == []
    assert client.search_calls == []


async def test_bare_default_policy_cannot_create_supplier() -> None:
    client = FakeJson2Client()
    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        await _writer(client, policy=OdooSupplierPartnerWritePolicy()).create_supplier(_command())
    assert client.create_calls == []
    assert client.search_calls == []


async def test_flag_off_policy_refuses_before_any_odoo_call() -> None:
    policy = OdooSupplierPartnerWritePolicy(supplier_remediation_write_enabled=False, app_env="staging")
    client = FakeJson2Client()
    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        await _writer(client, policy=policy).create_supplier(_command())
    assert client.create_calls == []
    assert client.search_calls == []


async def test_staging_gate_requires_named_approver() -> None:
    client = FakeJson2Client(search_results=[])
    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        await _writer(client).create_supplier(_command(approved_by=None))


async def test_production_gate_requires_full_acknowledgement() -> None:
    incomplete = OdooSupplierPartnerWritePolicy(
        supplier_remediation_write_enabled=True,
        app_env="production",
        production_operations_enabled=True,
        production_approval_ack="",
    )
    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        await _writer(FakeJson2Client(), policy=incomplete).create_supplier(_command())

    complete = OdooSupplierPartnerWritePolicy(
        supplier_remediation_write_enabled=True,
        app_env="production",
        production_operations_enabled=True,
        production_approval_ack=PRODUCTION_APPROVAL_ACK,
    )
    client = FakeJson2Client(search_results=[_partner_row(partner_id=88)])
    result = await _writer(client, policy=complete).create_supplier(_command())
    assert result.status is SupplierPartnerWriteStatus.ALREADY_EXISTS


# --------------------------------------------------------- Phase 30: create response shape
#
# The authoritative JSON-2 create-response shape rejection (bool / [] / [a,b] / dict / str)
# is asserted against the real client in
# test_odoo_client.py::test_create_res_partner_rejects_unexpected_response_shapes.
# Here we only confirm the writer fails closed on a non-positive id from its client.


async def test_create_returning_non_positive_id_is_data_integrity_error() -> None:
    client = FakeJson2Client(create_result=0, search_sequence=[[]])
    with pytest.raises(SupplierPartnerDataIntegrityError):
        await _writer(client).create_supplier(_command())


async def test_create_returning_bool_id_is_data_integrity_error() -> None:
    client = FakeJson2Client(create_result=True, search_sequence=[[]])
    with pytest.raises(SupplierPartnerDataIntegrityError):
        await _writer(client).create_supplier(_command())


# --------------------------------------------------------- Phase 31: RETRY / idempotency


async def test_retry_after_create_returns_already_exists_without_second_create() -> None:
    first_client = FakeJson2Client(create_result=123, search_sequence=[[], [_partner_row(partner_id=123)]])
    first = await _writer(first_client).create_supplier(_command())
    assert first.status is SupplierPartnerWriteStatus.CREATED
    assert len(first_client.create_calls) == 1

    retry_client = FakeJson2Client(search_results=[_partner_row(partner_id=123)])
    retry = await _writer(retry_client).create_supplier(_command())
    assert retry.status is SupplierPartnerWriteStatus.ALREADY_EXISTS
    assert retry.partner_id == 123
    assert retry_client.create_calls == []


# --------------------------------------------------------- Phase 32: DUPLICATE RACE


async def test_post_create_duplicate_recheck_fails_closed() -> None:
    client = FakeJson2Client(
        create_result=200,
        search_sequence=[[], [_partner_row(partner_id=200), _partner_row(partner_id=201)]],
    )
    with pytest.raises(SupplierPartnerDuplicateRaceError):
        await _writer(client).create_supplier(_command())
    assert len(client.create_calls) == 1  # the create happened, but success is never reported


async def test_post_create_readback_mismatch_is_data_integrity_error() -> None:
    client = FakeJson2Client(create_result=200, search_sequence=[[], [_partner_row(partner_id=999)]])
    with pytest.raises(SupplierPartnerDataIntegrityError):
        await _writer(client).create_supplier(_command())


# --------------------------------------------------------- Phase 33: minimal payload


async def test_create_payload_contains_only_sanctioned_keys() -> None:
    client = FakeJson2Client(create_result=1, search_sequence=[[], [_partner_row(partner_id=1)]])
    await _writer(client).create_supplier(_command())

    assert set(client.create_calls[0]) == {"name", "vat", "company_type"}
    forbidden = {
        "email",
        "phone",
        "mobile",
        "street",
        "street2",
        "city",
        "zip",
        "country_id",
        "state_id",
        "property_account_payable_id",
        "property_account_receivable_id",
        "property_payment_term_id",
        "property_supplier_payment_term_id",
        "bank_ids",
        "category_id",
        "user_id",
        "supplier_rank",
        "customer_rank",
        "currency_id",
        "property_account_position_id",
        "company_id",
    }
    assert forbidden.isdisjoint(client.create_calls[0])


# --------------------------------------------------------- Phase 34: security / no secret leakage


async def test_connector_errors_are_translated_without_leaking_secrets() -> None:
    client = FakeJson2Client(search_results=ConnectorAuthenticationError("Odoo authentication failed."))
    with pytest.raises(Exception) as exc_info:  # noqa: B017 - translated safe error
        await _writer(client).create_supplier(_command())
    message = getattr(exc_info.value, "safe_message", str(exc_info.value))
    for marker in ("api_key", "authorization", "bearer", "password", "token", "secret", SECRET_MARKER):
        assert marker not in message.lower()


def test_writer_module_never_touches_secret_material() -> None:
    source = Path("app/erp/write/odoo_supplier_partner_writer.py").read_text(encoding="utf-8").lower()
    for marker in ("odoo_api_key", "get_secret_value", "x-odoo-database", "authorization:", SECRET_MARKER.lower()):
        assert marker not in source


# --------------------------------------------------------- command DTO validation


@pytest.mark.parametrize(
    "overrides",
    [
        {"company_id": 0},
        {"company_id": True},
        {"supplier_name": "   "},
        {"supplier_tax_number": ""},
        {"idempotency_key": " "},
        {"approved_by": "  "},
    ],
)
def test_create_supplier_command_rejects_invalid_identity(overrides: dict[str, Any]) -> None:
    with pytest.raises(SupplierPartnerWriteValidationError):
        _command(**overrides)


def test_create_supplier_command_normalization_is_whitespace_strip_only() -> None:
    # Same semantics as PartnerMatchingEngine._clean: strip only, no prefix removal.
    from app.erp.write.odoo_supplier_partner_writer import _normalize_supplier_vat

    assert _normalize_supplier_vat("  0430367181 ") == "0430367181"
    assert _normalize_supplier_vat("TR0430367181") == "TR0430367181"


# --------------------------------------------------------- Phase 21/22/23: no wiring


def test_partner_matching_engine_never_creates_a_partner() -> None:
    source = Path("app/matching/partner.py").read_text(encoding="utf-8")
    for token in ("create", "SupplierPartnerWriter", "OdooSupplierPartnerWriter", "res.partner/create"):
        assert token not in source


@pytest.mark.parametrize(
    "module_path",
    [
        "app/application/use_cases/import_invoice.py",
        "app/application/use_cases/reclassify_review.py",
        "app/application/use_cases/review_classification_outcome.py",
        "app/application/rules/deterministic.py",
    ],
)
def test_import_and_reclassification_do_not_reference_the_supplier_writer(module_path: str) -> None:
    tree = ast.parse(Path(module_path).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for name in imported:
        assert "supplier_partner" not in name
        assert "SupplierPartnerWriter" not in name


def test_no_api_router_exposes_supplier_creation() -> None:
    for router in Path("app/api/routers").glob("*.py"):
        source = router.read_text(encoding="utf-8")
        assert "SupplierPartnerWriter" not in source
        assert "create_supplier" not in source
        assert "resolve-supplier" not in source
        assert "create-supplier" not in source
