from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.application.quotation import CreateCustomerQuotationCommand, CustomerQuotationDraft, CustomerQuotationLine
from app.connectors.exceptions import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorError,
    ConnectorTimeoutError,
    ConnectorValidationError,
)
from app.core.runtime_checks import PRODUCTION_APPROVAL_ACK
from app.erp.write import (
    CustomerQuotationWriteAuthenticationError,
    CustomerQuotationWriteAuthorizationError,
    CustomerQuotationWriteConfigurationError,
    CustomerQuotationWriteDuplicateError,
    CustomerQuotationWritePricelistError,
    CustomerQuotationWriteSafetyGateError,
    CustomerQuotationWriteTransportError,
    CustomerQuotationWriteUnexpectedErpError,
    CustomerQuotationWriteValidationError,
    OdooCustomerQuotationFieldMapping,
    OdooCustomerQuotationPricelistResolver,
    OdooCustomerQuotationRepository,
    OdooCustomerQuotationWritePolicy,
    OdooCustomerQuotationWriter,
    build_sale_order_payload,
)

KEY_FIELD = "x_studio_ict_hub_execution_key"


class FakeJson2Client:
    def __init__(
        self,
        *,
        create_result: Any = 8001,
        search_results: dict[str, Any] | None = None,
    ) -> None:
        self.create_result = create_result
        self.search_results = search_results if search_results is not None else {}
        self.create_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []

    async def create_sale_order(self, payload: dict[str, Any]) -> int:
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
        result = self.search_results.get(model, [])
        if isinstance(result, Exception):
            raise result
        return result


def _line(
    line_id: str = "line-1",
    *,
    product_variant_id: int = 10,
    quantity: str = "2.000",
    sales_unit_price: str = "10.00",
    description: str | None = "Widget",
    uom_id: int | None = None,
) -> CustomerQuotationLine:
    return CustomerQuotationLine(
        line_id=line_id,
        product_variant_id=product_variant_id,
        quantity=Decimal(quantity),
        sales_unit_price=Decimal(sales_unit_price),
        description=description,
        uom_id=uom_id,
    )


def _draft(*lines: CustomerQuotationLine, **overrides: object) -> CustomerQuotationDraft:
    values: dict[str, object] = {
        "company_id": 7,
        "customer_id": 501,
        "currency": "eur",
        "scenario_id": "scenario-a",
        "scenario_name": "Scenario A",
        "review_id": "review-1",
        "decision_id": "decision-1",
        "decision_version": 4,
        "lines": lines or (_line(),),
        "opportunity_id": 88,
    }
    values.update(overrides)
    return CustomerQuotationDraft(**values)


def _command(*lines: CustomerQuotationLine, **overrides: object) -> CreateCustomerQuotationCommand:
    return CreateCustomerQuotationCommand(draft=_draft(*lines, **overrides), approved_by="controller")


def _mapping(**overrides: object) -> OdooCustomerQuotationFieldMapping:
    values: dict[str, object] = {"execution_key_field": KEY_FIELD}
    values.update(overrides)
    return OdooCustomerQuotationFieldMapping(**values)


def _policy() -> OdooCustomerQuotationWritePolicy:
    return OdooCustomerQuotationWritePolicy(
        production_operations_enabled=True,
        production_approval_ack=PRODUCTION_APPROVAL_ACK,
        customer_quotation_execute_enabled=True,
    )


def _writer(
    client: FakeJson2Client,
    *,
    mapping: OdooCustomerQuotationFieldMapping | None = None,
    policy: OdooCustomerQuotationWritePolicy | None = None,
) -> OdooCustomerQuotationWriter:
    resolved_mapping = mapping or _mapping()
    return OdooCustomerQuotationWriter(
        repository=OdooCustomerQuotationRepository(client=client, mapping=resolved_mapping),
        pricelist_resolver=OdooCustomerQuotationPricelistResolver(client=client),
        policy=policy or _policy(),
    )


def _priced_client(*, create_result: Any = 8001, sale_order: Any = None) -> FakeJson2Client:
    return FakeJson2Client(
        create_result=create_result,
        search_results={
            "res.currency": [{"id": 42, "name": "EUR"}],
            "product.pricelist": [{"id": 9, "name": "EUR public", "currency_id": 42, "company_id": 7}],
            "sale.order": sale_order if sale_order is not None else [],
        },
    )


async def test_zero_existing_match_creates_one_draft_quotation() -> None:
    client = _priced_client(create_result=8001)
    result = await _writer(client).create_quotation(_command())

    assert result.created is True
    assert result.external_quotation_id == 8001
    assert len(client.create_calls) == 1
    payload = client.create_calls[0]
    assert payload["company_id"] == 7
    assert payload["partner_id"] == 501
    assert payload["pricelist_id"] == 9
    assert payload[KEY_FIELD] == _draft().execution_key


async def test_exact_one_existing_match_is_idempotent_without_create() -> None:
    client = _priced_client(sale_order=[{"id": 8001, "name": "S00042"}])
    result = await _writer(client).create_quotation(_command())

    assert result.created is False
    assert result.external_quotation_id == 8001
    assert result.external_reference == "S00042"
    assert client.create_calls == []


async def test_duplicate_existing_matches_fail_closed() -> None:
    client = _priced_client(sale_order=[{"id": 8001, "name": "A"}, {"id": 8002, "name": "B"}])
    with pytest.raises(CustomerQuotationWriteDuplicateError):
        await _writer(client).create_quotation(_command())
    assert client.create_calls == []


async def test_lookup_scopes_by_company_and_execution_key() -> None:
    client = _priced_client()
    await _writer(client).create_quotation(_command())

    lookup = next(call for call in client.search_calls if call["model"] == "sale.order")
    assert lookup["domain"] == [[KEY_FIELD, "=", _draft().execution_key], ["company_id", "=", 7]]
    assert lookup["limit"] == 2


async def test_partner_and_product_variant_mapping_are_direct() -> None:
    client = _priced_client()
    await _writer(client).create_quotation(_command(_line("l1", product_variant_id=9091)))

    payload = client.create_calls[0]
    assert payload["partner_id"] == 501
    assert payload["order_line"][0][2]["product_id"] == 9091


async def test_quantity_and_price_are_preserved_as_decimal_strings() -> None:
    client = _priced_client()
    await _writer(client).create_quotation(_command(_line("l1", quantity="2.000", sales_unit_price="123.4500")))

    line_vals = client.create_calls[0]["order_line"][0][2]
    assert line_vals["product_uom_qty"] == "2.000"
    assert line_vals["price_unit"] == "123.4500"
    assert not isinstance(line_vals["price_unit"], float)
    assert not isinstance(line_vals["product_uom_qty"], float)


async def test_zero_sales_price_is_preserved() -> None:
    client = _priced_client()
    await _writer(client).create_quotation(_command(_line("l1", sales_unit_price="0")))

    assert client.create_calls[0]["order_line"][0][2]["price_unit"] == "0"


async def test_uom_mapping_only_when_supplied() -> None:
    client = _priced_client()
    await _writer(client).create_quotation(_command(_line("with-uom", uom_id=5), _line("no-uom", uom_id=None)))

    lines = client.create_calls[0]["order_line"]
    assert lines[0][2]["product_uom_id"] == 5
    assert "product_uom_id" not in lines[1][2]


async def test_description_mapping_only_when_supplied() -> None:
    client = _priced_client()
    await _writer(client).create_quotation(
        _command(_line("with-desc", description="Managed service"), _line("no-desc", description=None))
    )

    lines = client.create_calls[0]["order_line"]
    assert lines[0][2]["name"] == "Managed service"
    assert "name" not in lines[1][2]


async def test_order_lines_use_one2many_command_shape_in_scenario_order() -> None:
    client = _priced_client()
    await _writer(client).create_quotation(
        _command(
            _line("l-a", product_variant_id=1), _line("l-b", product_variant_id=2), _line("l-c", product_variant_id=3)
        )
    )

    order_line = client.create_calls[0]["order_line"]
    assert [(command, zero) for command, zero, _vals in order_line] == [(0, 0), (0, 0), (0, 0)]
    assert [vals["product_id"] for _c, _z, vals in order_line] == [1, 2, 3]


async def test_payload_never_confirms_or_uses_client_order_ref() -> None:
    client = _priced_client()
    await _writer(client).create_quotation(_command())

    payload_text = str(client.create_calls[0])
    assert "action_confirm" not in payload_text
    assert "client_order_ref" not in payload_text
    assert "state" not in client.create_calls[0]


async def test_missing_execution_key_field_config_fails_closed_before_any_odoo_call() -> None:
    client = _priced_client()
    writer = _writer(client, mapping=_mapping(execution_key_field=None))

    with pytest.raises(CustomerQuotationWriteConfigurationError):
        await writer.create_quotation(_command())
    assert client.search_calls == []
    assert client.create_calls == []


async def test_client_order_ref_configured_as_execution_key_field_fails_closed() -> None:
    client = _priced_client()
    writer = _writer(client, mapping=_mapping(execution_key_field="client_order_ref"))

    with pytest.raises(CustomerQuotationWriteConfigurationError):
        await writer.create_quotation(_command())
    assert client.create_calls == []


async def test_missing_pricelist_fails_closed() -> None:
    client = FakeJson2Client(
        search_results={
            "res.currency": [{"id": 42, "name": "EUR"}],
            "product.pricelist": [],
            "sale.order": [],
        }
    )
    with pytest.raises(CustomerQuotationWritePricelistError):
        await _writer(client).create_quotation(_command())
    assert client.create_calls == []


async def test_ambiguous_pricelist_fails_closed() -> None:
    client = FakeJson2Client(
        search_results={
            "res.currency": [{"id": 42, "name": "EUR"}],
            "product.pricelist": [{"id": 9, "name": "A"}, {"id": 10, "name": "B"}],
            "sale.order": [],
        }
    )
    with pytest.raises(CustomerQuotationWritePricelistError):
        await _writer(client).create_quotation(_command())


async def test_missing_or_ambiguous_currency_fails_closed() -> None:
    for currency_records in ([], [{"id": 1, "name": "EUR"}, {"id": 2, "name": "EUR"}]):
        client = FakeJson2Client(
            search_results={
                "res.currency": currency_records,
                "product.pricelist": [{"id": 9}],
                "sale.order": [],
            }
        )
        with pytest.raises(CustomerQuotationWritePricelistError):
            await _writer(client).create_quotation(_command())


async def test_supported_create_response_id_is_returned() -> None:
    client = _priced_client(create_result=8123)
    result = await _writer(client).create_quotation(_command())
    assert result.external_quotation_id == 8123


async def test_unexpected_create_response_is_rejected_safely() -> None:
    client = _priced_client(
        create_result=ConnectorError("Odoo sale.order create returned an unexpected response shape.")
    )
    with pytest.raises(CustomerQuotationWriteUnexpectedErpError):
        await _writer(client).create_quotation(_command())


async def test_invalid_create_id_value_is_rejected() -> None:
    client = _priced_client(create_result=0)
    with pytest.raises(CustomerQuotationWriteUnexpectedErpError):
        await _writer(client).create_quotation(_command())


@pytest.mark.parametrize(
    ("error", "expected"),
    (
        (ConnectorAuthenticationError("auth"), CustomerQuotationWriteAuthenticationError),
        (ConnectorAuthorizationError("forbidden"), CustomerQuotationWriteAuthorizationError),
        (ConnectorValidationError("bad payload"), CustomerQuotationWriteValidationError),
        (ConnectorTimeoutError("timeout"), CustomerQuotationWriteTransportError),
        (ConnectorError("boom"), CustomerQuotationWriteUnexpectedErpError),
    ),
)
async def test_connector_errors_are_translated_to_safe_writer_errors(
    error: Exception,
    expected: type[Exception],
) -> None:
    client = _priced_client(create_result=error)
    with pytest.raises(expected):
        await _writer(client).create_quotation(_command())


async def test_production_safety_gate_blocks_write_without_approval() -> None:
    client = _priced_client()
    strict = OdooCustomerQuotationWriter(
        repository=OdooCustomerQuotationRepository(client=client, mapping=_mapping()),
        pricelist_resolver=OdooCustomerQuotationPricelistResolver(client=client),
        policy=OdooCustomerQuotationWritePolicy(
            production_operations_enabled=True,
            production_approval_ack=PRODUCTION_APPROVAL_ACK,
            customer_quotation_execute_enabled=False,
        ),
    )
    with pytest.raises(CustomerQuotationWriteSafetyGateError):
        await strict.create_quotation(_command())
    assert client.create_calls == []


async def test_opportunity_and_source_reference_only_written_when_configured() -> None:
    client = _priced_client()
    await _writer(client).create_quotation(_command())
    assert "opportunity_id" not in client.create_calls[0]
    assert all(key != "x_ref" for key in client.create_calls[0])

    client2 = _priced_client()
    writer2 = _writer(
        client2, mapping=_mapping(opportunity_field="opportunity_id", source_reference_field="x_studio_source_ref")
    )
    await writer2.create_quotation(_command())
    payload = client2.create_calls[0]
    assert payload["opportunity_id"] == 88
    assert payload["x_studio_source_ref"] == "Scenario A"
    assert payload["x_studio_source_ref"] != payload[KEY_FIELD]


def test_build_sale_order_payload_shape_is_stable() -> None:
    payload = build_sale_order_payload(
        _command(_line("l-a", product_variant_id=1), _line("l-b", product_variant_id=2)),
        pricelist_id=9,
        execution_key="quotation-scenario-execution:abc",
        mapping=_mapping(),
    )
    assert payload["order_line"] == [
        (0, 0, {"product_id": 1, "product_uom_qty": "2.000", "price_unit": "10.00", "name": "Widget"}),
        (0, 0, {"product_id": 2, "product_uom_qty": "2.000", "price_unit": "10.00", "name": "Widget"}),
    ]
    assert "cost_unit_price" not in str(payload)
    assert "standard_price" not in str(payload)
    assert "tax_id" not in str(payload)


def test_writer_module_does_not_reuse_new_rfq_purchase_or_proposal_reader_or_register_strategy() -> None:
    source = Path("app/erp/write/odoo_customer_quotation_writer.py").read_text(encoding="utf-8")
    lowered = source.lower()
    for token in (
        "new_rfq_purchase",
        "proposal",
        "get_scenario",
        "quotationscenariosourcereader",
        "executionstrategyresolver",
        "executionplanner",
        "executionsteptype",
        "create_studio_record",
        "ir.model.fields",
        "subscription",
        "plan_id",
        "recurring",
        "cost_unit_price",
        "standard_price",
        "purchase_price",
    ):
        assert token not in lowered


def test_writer_module_has_no_sqlalchemy_or_model_imports() -> None:
    tree = ast.parse(Path("app/erp/write/odoo_customer_quotation_writer.py").read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    assert not any(module.startswith(("sqlalchemy", "app.models")) for module in modules)
