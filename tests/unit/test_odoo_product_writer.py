"""Controlled Odoo product.template writer (P0-PROD-07F).

A narrow, gated capability to create exactly one ``product.template`` (a simple
no-attribute product) and deterministically resolve its single ``product.product``
variant id. ``ProductMatchingEngine`` stays read-only; this writer is the only
sanctioned ``product.template`` create path and is impossible to reach with
default settings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.application.commands import CreateProductCommand
from app.application.dto import ProductWriteStatus
from app.application.exceptions.product_remediation import (
    ProductDataIntegrityError,
    ProductVariantResolutionError,
    ProductWriteSafetyGateError,
    ProductWriteValidationError,
)
from app.connectors.exceptions import ConnectorAuthenticationError
from app.core.config import Settings
from app.core.runtime_checks import PRODUCTION_APPROVAL_ACK
from app.erp.write import (
    OdooProductTemplateRepository,
    OdooProductWritePolicy,
    OdooProductWriter,
)

STAGING_HOST_URL = "https://test-ictteknoloji.odoo.com"
SECRET_MARKER = "sk-super-secret-odoo-key"


class FakeProductJson2Client:
    def __init__(
        self,
        *,
        create_result: Any = 900,
        search_sequence: list[Any] | None = None,
    ) -> None:
        self.create_result = create_result
        self.search_sequence = list(search_sequence) if search_sequence is not None else []
        self.create_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []

    async def create_product_template(self, payload: dict[str, Any]) -> int:
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


def _template_row(*, template_id: int = 900, name: str = "Test Product", default_code: Any = False) -> dict[str, Any]:
    return {"id": template_id, "name": name, "default_code": default_code, "type": "consu", "uom_id": [1, "Units"]}


def _variant_row(*, variant_id: int = 9500) -> dict[str, Any]:
    return {"id": variant_id}


def _command(**overrides: Any) -> CreateProductCommand:
    kwargs: dict[str, Any] = {
        "name": "Test Product",
        "type": "consu",
        "uom_id": 1,
        "is_storable": True,
        "approved_by": "finance.operator",
    }
    kwargs.update(overrides)
    return CreateProductCommand(**kwargs)


def _enabled_policy() -> OdooProductWritePolicy:
    return OdooProductWritePolicy(
        product_remediation_write_enabled=True,
        app_env="staging",
        odoo_host="test-ictteknoloji.odoo.com",
    )


def _writer(client: FakeProductJson2Client, *, policy: OdooProductWritePolicy | None = None) -> OdooProductWriter:
    return OdooProductWriter(
        repository=OdooProductTemplateRepository(client=client),
        policy=policy if policy is not None else _enabled_policy(),
    )


# --------------------------------------------------------- A/B/C/D: gate


def test_default_settings_have_product_remediation_write_disabled() -> None:
    assert Settings().product_remediation_write_enabled is False


async def test_default_settings_cannot_create_product() -> None:
    default_policy = OdooProductWritePolicy.from_settings(Settings())
    assert default_policy.product_remediation_write_enabled is False
    client = FakeProductJson2Client()
    with pytest.raises(ProductWriteSafetyGateError):
        await _writer(client, policy=default_policy).create_product(_command())
    assert client.create_calls == []
    assert client.search_calls == []


async def test_bare_default_policy_cannot_create_product() -> None:
    client = FakeProductJson2Client()
    with pytest.raises(ProductWriteSafetyGateError):
        await _writer(client, policy=OdooProductWritePolicy()).create_product(_command())
    assert client.create_calls == []


async def test_flag_off_policy_refuses_before_any_odoo_call() -> None:
    policy = OdooProductWritePolicy(product_remediation_write_enabled=False, app_env="staging")
    client = FakeProductJson2Client()
    with pytest.raises(ProductWriteSafetyGateError):
        await _writer(client, policy=policy).create_product(_command())
    assert client.create_calls == []


def test_policy_never_reads_execution_or_supplier_remediation_gates() -> None:
    import dataclasses

    field_names = {field.name for field in dataclasses.fields(OdooProductWritePolicy)}
    assert "execution_execute_enabled" not in field_names
    assert "supplier_remediation_write_enabled" not in field_names


async def test_generic_production_operations_flag_alone_cannot_authorize_product_write() -> None:
    # Generic production enablement without product_remediation_write_enabled must never authorize.
    policy = OdooProductWritePolicy(
        product_remediation_write_enabled=False,
        app_env="production",
        production_operations_enabled=True,
        production_approval_ack=PRODUCTION_APPROVAL_ACK,
    )
    client = FakeProductJson2Client()
    with pytest.raises(ProductWriteSafetyGateError):
        await _writer(client, policy=policy).create_product(_command())
    assert client.create_calls == []


async def test_staging_gate_requires_named_approver() -> None:
    client = FakeProductJson2Client(search_sequence=[[_template_row()], [_variant_row()]])
    with pytest.raises(ProductWriteSafetyGateError):
        await _writer(client).create_product(_command(approved_by=None))


async def test_production_gate_requires_full_acknowledgement() -> None:
    incomplete = OdooProductWritePolicy(
        product_remediation_write_enabled=True,
        app_env="production",
        production_operations_enabled=True,
        production_approval_ack="",
    )
    with pytest.raises(ProductWriteSafetyGateError):
        await _writer(FakeProductJson2Client(), policy=incomplete).create_product(_command())

    complete = OdooProductWritePolicy(
        product_remediation_write_enabled=True,
        app_env="production",
        production_operations_enabled=True,
        production_approval_ack=PRODUCTION_APPROVAL_ACK,
    )
    client = FakeProductJson2Client(search_sequence=[[_template_row()], [_variant_row()]])
    result = await _writer(client, policy=complete).create_product(_command())
    assert result.status is ProductWriteStatus.CREATED


# --------------------------------------------------------- E/F/G: identity separation


async def test_create_payload_never_derives_default_code_from_anything() -> None:
    client = FakeProductJson2Client(
        create_result=900, search_sequence=[[_template_row(default_code=False)], [_variant_row()]]
    )
    await _writer(client).create_product(_command())

    assert client.create_calls[0] == {"name": "Test Product", "type": "consu", "uom_id": 1, "is_storable": True}
    assert "default_code" not in client.create_calls[0]


async def test_blank_internal_reference_remains_blank() -> None:
    client = FakeProductJson2Client(
        create_result=900, search_sequence=[[_template_row(default_code=False)], [_variant_row()]]
    )
    result = await _writer(client).create_product(_command(default_code=None))

    assert result.default_code is None
    assert "default_code" not in client.create_calls[0]


async def test_explicit_internal_reference_is_preserved_exactly() -> None:
    client = FakeProductJson2Client(
        create_result=900,
        search_sequence=[[_template_row(default_code="ICT-0001")], [_variant_row()]],
    )
    result = await _writer(client).create_product(_command(default_code="ICT-0001"))

    assert result.default_code == "ICT-0001"
    assert client.create_calls[0]["default_code"] == "ICT-0001"


async def test_create_product_command_has_no_supplier_code_field() -> None:
    # A supplier's own product code must never reach the product.template create command.
    import dataclasses

    field_names = {field.name for field in dataclasses.fields(CreateProductCommand)}
    assert "seller_item_code" not in field_names
    assert "product_code" not in field_names


# --------------------------------------------------------- H/I: variant retrieval


async def test_simple_template_read_back_yields_exactly_one_product_id() -> None:
    client = FakeProductJson2Client(
        create_result=900, search_sequence=[[_template_row()], [_variant_row(variant_id=9500)]]
    )
    result = await _writer(client).create_product(_command())

    assert result.status is ProductWriteStatus.CREATED
    assert result.template_id == 900
    assert result.product_id == 9500


async def test_zero_variants_fails_closed() -> None:
    client = FakeProductJson2Client(create_result=900, search_sequence=[[_template_row()], []])
    with pytest.raises(ProductVariantResolutionError):
        await _writer(client).create_product(_command())


async def test_multiple_variants_fails_closed() -> None:
    client = FakeProductJson2Client(
        create_result=900,
        search_sequence=[[_template_row()], [_variant_row(variant_id=1), _variant_row(variant_id=2)]],
    )
    with pytest.raises(ProductVariantResolutionError):
        await _writer(client).create_product(_command())


async def test_template_cannot_be_read_back_fails_closed() -> None:
    client = FakeProductJson2Client(create_result=900, search_sequence=[[]])
    with pytest.raises(ProductDataIntegrityError):
        await _writer(client).create_product(_command())


async def test_template_read_back_default_code_mismatch_fails_closed() -> None:
    client = FakeProductJson2Client(
        create_result=900, search_sequence=[[_template_row(default_code="WRONG-CODE")], [_variant_row()]]
    )
    with pytest.raises(ProductDataIntegrityError):
        await _writer(client).create_product(_command(default_code="ICT-0001"))


# --------------------------------------------------------- create response shape


async def test_create_returning_non_positive_id_is_data_integrity_error() -> None:
    client = FakeProductJson2Client(create_result=0)
    with pytest.raises(ProductDataIntegrityError):
        await _writer(client).create_product(_command())


async def test_create_returning_bool_id_is_data_integrity_error() -> None:
    client = FakeProductJson2Client(create_result=True)
    with pytest.raises(ProductDataIntegrityError):
        await _writer(client).create_product(_command())


# --------------------------------------------------------- minimal payload / forbidden fields


async def test_create_payload_contains_only_sanctioned_keys() -> None:
    client = FakeProductJson2Client(
        create_result=900,
        search_sequence=[[_template_row(default_code="ICT-0001")], [_variant_row()]],
    )
    await _writer(client).create_product(_command(default_code="ICT-0001"))

    assert set(client.create_calls[0]) == {"name", "type", "uom_id", "is_storable", "default_code"}
    forbidden = {
        "categ_id",
        "taxes_id",
        "supplier_taxes_id",
        "barcode",
        "company_id",
        "seller_ids",
        "x_studio_ana_tedarikci",
    }
    assert forbidden.isdisjoint(client.create_calls[0])


# --------------------------------------------------------- security / no secret leakage


async def test_connector_errors_are_translated_without_leaking_secrets() -> None:
    client = FakeProductJson2Client(search_sequence=[ConnectorAuthenticationError("Odoo authentication failed.")])
    with pytest.raises(Exception) as exc_info:  # noqa: B017 - translated safe error
        await _writer(client).create_product(_command())
    message = getattr(exc_info.value, "safe_message", str(exc_info.value))
    for marker in ("api_key", "authorization", "bearer", "password", "token", "secret", SECRET_MARKER):
        assert marker not in message.lower()


def test_writer_module_never_touches_secret_material() -> None:
    source = Path("app/erp/write/odoo_product_writer.py").read_text(encoding="utf-8").lower()
    for marker in ("odoo_api_key", "get_secret_value", "x-odoo-database", "authorization:", SECRET_MARKER.lower()):
        assert marker not in source


def test_writer_module_never_writes_studio_field() -> None:
    source = Path("app/erp/write/odoo_product_writer.py").read_text(encoding="utf-8")
    assert "x_studio_ana_tedarikci" not in source


# --------------------------------------------------------- command DTO validation


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "   "},
        {"type": "not_a_real_type"},
        {"uom_id": 0},
        {"uom_id": True},
        {"is_storable": "yes"},
        {"default_code": "   "},
        {"approved_by": "  "},
    ],
)
def test_create_product_command_rejects_invalid_identity(overrides: dict[str, Any]) -> None:
    with pytest.raises(ProductWriteValidationError):
        _command(**overrides)


# --------------------------------------------------------- Q: no wiring / no regressions


def test_product_matching_engine_never_creates_a_product() -> None:
    source = Path("app/matching/product.py").read_text(encoding="utf-8")
    for token in ("create", "ProductWriter", "OdooProductWriter", "product.template/create"):
        assert token not in source


def test_product_matching_engine_still_uses_default_code_barcode_and_seller_item_code() -> None:
    # ManageEngine relies on: buyer_item_code -> default_code, barcode -> barcode,
    # seller_item_code -> default_code. This must not regress.
    source = Path("app/matching/product.py").read_text(encoding="utf-8")
    assert "find_by_default_code" in source
    assert "find_by_barcode" in source
    assert "seller_item_code" in source


def test_no_api_router_exposes_product_creation() -> None:
    for router in Path("app/api/routers").glob("*.py"):
        source = router.read_text(encoding="utf-8")
        assert "OdooProductWriter" not in source
        assert "create_product" not in source
        assert "create-product" not in source
