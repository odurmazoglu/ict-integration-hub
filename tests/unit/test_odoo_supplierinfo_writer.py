"""Controlled Odoo product.supplierinfo writer (P0-PROD-07F).

A narrow, gated, vendor/code-idempotent capability to create exactly one
``product.supplierinfo`` link. Supplier identity (``partner_id`` + ``product_code``)
is strictly separate from ICT product identity (``product.template.default_code``):
this writer never mutates ``default_code``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.application.commands import CreateSupplierInfoCommand
from app.application.dto import SupplierInfoWriteStatus
from app.application.exceptions.product_remediation import (
    ProductWriteSafetyGateError,
    SupplierInfoAmbiguityError,
    SupplierInfoDataIntegrityError,
    SupplierInfoDuplicateRaceError,
    SupplierInfoWriteValidationError,
)
from app.connectors.exceptions import ConnectorAuthenticationError
from app.core.config import Settings
from app.core.runtime_checks import PRODUCTION_APPROVAL_ACK
from app.erp.write import (
    OdooProductWritePolicy,
    OdooSupplierInfoRepository,
    OdooSupplierInfoWriter,
)

COMPANY_ID = 1
OTHER_COMPANY_ID = 2
PARTNER_ID = 55
OTHER_PARTNER_ID = 56
PRODUCT_TMPL_ID = 900
PRODUCT_CODE = "HBV000006MHLQ"
SECRET_MARKER = "sk-super-secret-odoo-key"


class FakeSupplierInfoJson2Client:
    def __init__(
        self,
        *,
        create_result: Any = 4001,
        search_sequence: list[Any] | None = None,
    ) -> None:
        self.create_result = create_result
        self.search_sequence = list(search_sequence) if search_sequence is not None else []
        self.create_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []

    async def create_supplierinfo(self, payload: dict[str, Any]) -> int:
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
        result = self.search_sequence.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _row(
    *,
    supplierinfo_id: int = 4001,
    partner_id: Any = PARTNER_ID,
    product_tmpl_id: Any = PRODUCT_TMPL_ID,
    product_id: Any = False,
    product_code: str = PRODUCT_CODE,
    company_id: Any = COMPANY_ID,
) -> dict[str, Any]:
    return {
        "id": supplierinfo_id,
        "partner_id": partner_id,
        "product_tmpl_id": product_tmpl_id,
        "product_id": product_id,
        "product_code": product_code,
        "company_id": company_id,
    }


def _command(**overrides: Any) -> CreateSupplierInfoCommand:
    kwargs: dict[str, Any] = {
        "company_id": COMPANY_ID,
        "partner_id": PARTNER_ID,
        "product_tmpl_id": PRODUCT_TMPL_ID,
        "product_code": PRODUCT_CODE,
        "idempotency_key": "product-remediation:1:HBV000006MHLQ",
        "approved_by": "finance.operator",
    }
    kwargs.update(overrides)
    return CreateSupplierInfoCommand(**kwargs)


def _enabled_policy() -> OdooProductWritePolicy:
    return OdooProductWritePolicy(
        product_remediation_write_enabled=True,
        app_env="staging",
        odoo_host="test-ictteknoloji.odoo.com",
    )


def _writer(
    client: FakeSupplierInfoJson2Client, *, policy: OdooProductWritePolicy | None = None
) -> OdooSupplierInfoWriter:
    return OdooSupplierInfoWriter(
        repository=OdooSupplierInfoRepository(client=client),
        policy=policy if policy is not None else _enabled_policy(),
    )


# --------------------------------------------------------- A/B/C/D: gate


async def test_default_settings_cannot_create_supplierinfo() -> None:
    default_policy = OdooProductWritePolicy.from_settings(Settings())
    assert default_policy.product_remediation_write_enabled is False
    client = FakeSupplierInfoJson2Client()
    with pytest.raises(ProductWriteSafetyGateError):
        await _writer(client, policy=default_policy).create_supplier_info(_command())
    assert client.create_calls == []
    assert client.search_calls == []


async def test_generic_production_operations_flag_alone_cannot_authorize_supplierinfo_write() -> None:
    policy = OdooProductWritePolicy(
        product_remediation_write_enabled=False,
        app_env="production",
        production_operations_enabled=True,
        production_approval_ack=PRODUCTION_APPROVAL_ACK,
    )
    client = FakeSupplierInfoJson2Client()
    with pytest.raises(ProductWriteSafetyGateError):
        await _writer(client, policy=policy).create_supplier_info(_command())
    assert client.create_calls == []


async def test_staging_gate_requires_named_approver() -> None:
    client = FakeSupplierInfoJson2Client(search_sequence=[[]])
    with pytest.raises(ProductWriteSafetyGateError):
        await _writer(client).create_supplier_info(_command(approved_by=None))


async def test_production_gate_requires_full_acknowledgement() -> None:
    incomplete = OdooProductWritePolicy(
        product_remediation_write_enabled=True,
        app_env="production",
        production_operations_enabled=True,
        production_approval_ack="",
    )
    with pytest.raises(ProductWriteSafetyGateError):
        await _writer(FakeSupplierInfoJson2Client(), policy=incomplete).create_supplier_info(_command())

    complete = OdooProductWritePolicy(
        product_remediation_write_enabled=True,
        app_env="production",
        production_operations_enabled=True,
        production_approval_ack=PRODUCTION_APPROVAL_ACK,
    )
    client = FakeSupplierInfoJson2Client(search_sequence=[[_row()]])
    result = await _writer(client, policy=complete).create_supplier_info(_command())
    assert result.status is SupplierInfoWriteStatus.ALREADY_EXISTS


# --------------------------------------------------------- CREATED / ALREADY_EXISTS


async def test_creates_supplierinfo_when_no_identity_match() -> None:
    client = FakeSupplierInfoJson2Client(create_result=4001, search_sequence=[[], [_row(supplierinfo_id=4001)]])
    result = await _writer(client).create_supplier_info(_command())

    assert result.status is SupplierInfoWriteStatus.CREATED
    assert result.supplierinfo_id == 4001
    assert result.partner_id == PARTNER_ID
    assert result.product_tmpl_id == PRODUCT_TMPL_ID
    assert result.product_code == PRODUCT_CODE
    assert len(client.create_calls) == 1
    assert client.create_calls[0] == {
        "partner_id": PARTNER_ID,
        "product_tmpl_id": PRODUCT_TMPL_ID,
        "product_code": PRODUCT_CODE,
        "company_id": COMPANY_ID,
    }


async def test_returns_already_exists_for_single_identity_match() -> None:
    client = FakeSupplierInfoJson2Client(search_sequence=[[_row(supplierinfo_id=42)]])
    result = await _writer(client).create_supplier_info(_command())

    assert result.status is SupplierInfoWriteStatus.ALREADY_EXISTS
    assert result.supplierinfo_id == 42
    assert client.create_calls == []


# --------------------------------------------------------- J/K/L/M: identity lookup semantics


async def test_lookup_domain_includes_vendor_product_code_and_company_compatibility() -> None:
    client = FakeSupplierInfoJson2Client(search_sequence=[[], [_row()]])
    await _writer(client).create_supplier_info(_command())

    assert client.search_calls[0]["domain"] == [
        ["partner_id", "=", PARTNER_ID],
        ["product_code", "=", PRODUCT_CODE],
        ["company_id", "in", [COMPANY_ID, False]],
    ]


async def test_same_product_code_under_different_vendor_does_not_collide() -> None:
    # The fake client's search_read is a stand-in for Odoo's own domain filtering: a
    # vendor-scoped query for OTHER_PARTNER_ID never returns PARTNER_ID's record, so an
    # empty result here demonstrates no collision across vendors for the same code.
    client = FakeSupplierInfoJson2Client(
        create_result=5001, search_sequence=[[], [_row(supplierinfo_id=5001, partner_id=OTHER_PARTNER_ID)]]
    )
    result = await _writer(client).create_supplier_info(_command(partner_id=OTHER_PARTNER_ID))

    assert result.status is SupplierInfoWriteStatus.CREATED
    assert client.search_calls[0]["domain"][0] == ["partner_id", "=", OTHER_PARTNER_ID]


async def test_same_product_code_under_different_company_does_not_incorrectly_collide() -> None:
    # A specific (non-shared) record scoped to a different company must not match this
    # company's lookup: domain is ["company_id", "in", [company_id, False]], which
    # excludes another company's specific company_id.
    client = FakeSupplierInfoJson2Client(create_result=6001, search_sequence=[[], [_row(supplierinfo_id=6001)]])
    result = await _writer(client).create_supplier_info(_command(company_id=COMPANY_ID))

    assert result.status is SupplierInfoWriteStatus.CREATED
    assert client.search_calls[0]["domain"][2] == ["company_id", "in", [COMPANY_ID, False]]
    assert OTHER_COMPANY_ID not in client.search_calls[0]["domain"][2][2]


async def test_shared_company_record_is_matched_explicitly() -> None:
    # company_id=False on the existing record represents Odoo shared data; it must be
    # found regardless of which specific company_id the command carries.
    client = FakeSupplierInfoJson2Client(search_sequence=[[_row(supplierinfo_id=77, company_id=False)]])
    result = await _writer(client).create_supplier_info(_command(company_id=COMPANY_ID))

    assert result.status is SupplierInfoWriteStatus.ALREADY_EXISTS
    assert result.supplierinfo_id == 77
    assert client.create_calls == []


async def test_multiple_identity_matches_fail_closed() -> None:
    client = FakeSupplierInfoJson2Client(search_sequence=[[_row(supplierinfo_id=1), _row(supplierinfo_id=2)]])
    with pytest.raises(SupplierInfoAmbiguityError):
        await _writer(client).create_supplier_info(_command())
    assert client.create_calls == []


async def test_existing_match_for_different_product_fails_closed() -> None:
    client = FakeSupplierInfoJson2Client(search_sequence=[[_row(supplierinfo_id=88, product_tmpl_id=999)]])
    with pytest.raises(SupplierInfoDataIntegrityError):
        await _writer(client).create_supplier_info(_command())
    assert client.create_calls == []


# --------------------------------------------------------- N: never mutates default_code


async def test_create_payload_never_includes_default_code() -> None:
    client = FakeSupplierInfoJson2Client(create_result=4001, search_sequence=[[], [_row()]])
    await _writer(client).create_supplier_info(_command())

    assert "default_code" not in client.create_calls[0]


async def test_create_uses_resolved_partner_and_supplier_product_code() -> None:
    client = FakeSupplierInfoJson2Client(create_result=4001, search_sequence=[[], [_row()]])
    await _writer(client).create_supplier_info(_command())

    assert client.create_calls[0]["partner_id"] == PARTNER_ID
    assert client.create_calls[0]["product_code"] == PRODUCT_CODE


# --------------------------------------------------------- O: post-create verification


async def test_post_create_readback_verifies_identity() -> None:
    client = FakeSupplierInfoJson2Client(create_result=4001, search_sequence=[[], [_row(supplierinfo_id=4001)]])
    result = await _writer(client).create_supplier_info(_command())

    assert result.status is SupplierInfoWriteStatus.CREATED
    assert len(client.search_calls) == 2


async def test_post_create_duplicate_recheck_fails_closed() -> None:
    client = FakeSupplierInfoJson2Client(
        create_result=200,
        search_sequence=[[], [_row(supplierinfo_id=200), _row(supplierinfo_id=201)]],
    )
    with pytest.raises(SupplierInfoDuplicateRaceError):
        await _writer(client).create_supplier_info(_command())
    assert len(client.create_calls) == 1  # the create happened, but success is never reported


async def test_post_create_readback_mismatch_is_data_integrity_error() -> None:
    client = FakeSupplierInfoJson2Client(create_result=200, search_sequence=[[], [_row(supplierinfo_id=999)]])
    with pytest.raises(SupplierInfoDataIntegrityError):
        await _writer(client).create_supplier_info(_command())


# --------------------------------------------------------- P: partial-write safety


def test_supplierinfo_writer_never_calls_product_template_create() -> None:
    # There must be no code path in the supplierinfo module that creates (or retries
    # creating) a product.template -- the future orchestration (07G) must persist the
    # first product's identity and retry only supplierinfo on partial-write failure.
    source = Path("app/erp/write/odoo_supplierinfo_writer.py").read_text(encoding="utf-8")
    assert "create_product_template" not in source
    assert "OdooProductWriter" not in source


# --------------------------------------------------------- security / no secret leakage


async def test_connector_errors_are_translated_without_leaking_secrets() -> None:
    client = FakeSupplierInfoJson2Client(search_sequence=[ConnectorAuthenticationError("Odoo authentication failed.")])
    with pytest.raises(Exception) as exc_info:  # noqa: B017 - translated safe error
        await _writer(client).create_supplier_info(_command())
    message = getattr(exc_info.value, "safe_message", str(exc_info.value))
    for marker in ("api_key", "authorization", "bearer", "password", "token", "secret", SECRET_MARKER):
        assert marker not in message.lower()


def test_writer_module_never_touches_secret_material() -> None:
    source = Path("app/erp/write/odoo_supplierinfo_writer.py").read_text(encoding="utf-8").lower()
    for marker in ("odoo_api_key", "get_secret_value", "x-odoo-database", "authorization:", SECRET_MARKER.lower()):
        assert marker not in source


# --------------------------------------------------------- command DTO validation


@pytest.mark.parametrize(
    "overrides",
    [
        {"company_id": 0},
        {"partner_id": True},
        {"product_tmpl_id": 0},
        {"product_code": ""},
        {"idempotency_key": " "},
        {"approved_by": "  "},
        {"product_id": 0},
        {"currency_id": True},
        {"delay": 1.5},
        {"min_qty": -1},
        {"price": -0.01},
    ],
)
def test_create_supplier_info_command_rejects_invalid_identity(overrides: dict[str, Any]) -> None:
    with pytest.raises(SupplierInfoWriteValidationError):
        _command(**overrides)


def test_create_supplier_info_command_has_no_default_code_field() -> None:
    import dataclasses

    field_names = {field.name for field in dataclasses.fields(CreateSupplierInfoCommand)}
    assert "default_code" not in field_names


# --------------------------------------------------------- Q: no wiring / no regressions


def test_no_api_router_exposes_supplierinfo_creation() -> None:
    for router in Path("app/api/routers").glob("*.py"):
        source = router.read_text(encoding="utf-8")
        assert "OdooSupplierInfoWriter" not in source
        assert "create_supplier_info" not in source
