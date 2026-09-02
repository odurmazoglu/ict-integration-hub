import json

import httpx
import pytest

from app.connectors.exceptions import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorError,
    ConnectorValidationError,
)
from app.connectors.odoo.client import OdooJson2Client


async def test_create_account_move_returns_created_id() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/json/2/account.move/create"
        assert b"action_post" not in request.content
        return httpx.Response(200, json=123)

    client = OdooJson2Client(
        base_url="https://example.odoo.com",
        database="example",
        api_key="secret",
        timeout_seconds=10,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.odoo.com"),
    )

    assert await client.create_account_move({"move_type": "in_invoice"}) == 123


async def test_create_account_move_rejects_unexpected_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    client = OdooJson2Client(
        base_url="https://example.odoo.com",
        database="example",
        api_key="secret",
        timeout_seconds=10,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.odoo.com"),
    )

    with pytest.raises(ConnectorError) as exc_info:
        await client.create_account_move({"move_type": "in_invoice"})

    assert exc_info.value.safe_message == "Odoo account.move create returned an unexpected response."


async def test_call_model_method_uses_purchase_order_action_route() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/json/2/purchase.order/action_create_invoice"
        assert b'"ids":[42]' in request.content
        assert b'"kwargs":{}' in request.content
        return httpx.Response(200, json={"res_id": 901})

    client = OdooJson2Client(
        base_url="https://example.odoo.com",
        database="example",
        api_key="secret",
        timeout_seconds=10,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.odoo.com"),
    )

    assert await client.call_model_method(model="purchase.order", method="action_create_invoice", ids=[42]) == {
        "res_id": 901
    }


async def test_write_account_move_uses_vendor_bill_write_route() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/json/2/account.move/write"
        assert b'"ids":[901]' in request.content
        assert b'"values":{"ref":"INV-1001"}' in request.content
        return httpx.Response(200, json=True)

    client = OdooJson2Client(
        base_url="https://example.odoo.com",
        database="example",
        api_key="secret",
        timeout_seconds=10,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.odoo.com"),
    )

    assert await client.write_account_move(record_id=901, values={"ref": "INV-1001"}) is True


@pytest.mark.parametrize(
    ("status_code", "expected", "safe_message"),
    [
        (401, ConnectorAuthenticationError, "Odoo authentication failed."),
        (403, ConnectorAuthorizationError, "Odoo authorization failed."),
        (400, ConnectorValidationError, "Odoo rejected the request payload."),
        (422, ConnectorValidationError, "Odoo rejected the request payload."),
    ],
)
async def test_create_account_move_translates_http_status(
    status_code: int,
    expected: type[ConnectorError],
    safe_message: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "unsafe details"})

    client = OdooJson2Client(
        base_url="https://example.odoo.com",
        database="example",
        api_key="secret",
        timeout_seconds=10,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.odoo.com"),
    )

    with pytest.raises(expected) as exc_info:
        await client.create_account_move({"move_type": "in_invoice"})

    assert exc_info.value.safe_message == safe_message


@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (
            500,
            {"message": "Required field x_name is missing."},
            "Odoo returned HTTP 500. Required field x_name is missing.",
        ),
        (500, {"error": {"message": "Studio create failed."}}, "Odoo returned HTTP 500. Studio create failed."),
        (500, {"message": "Traceback: secret details", "debug": "api_key=secret"}, "Odoo returned HTTP 500."),
        (500, {"message": "failure api_key=super-secret"}, "Odoo returned HTTP 500."),
        (500, {"error": {"message": "Bearer abc123 rejected"}}, "Odoo returned HTTP 500."),
        (500, {"api_key": "secret", "token": "secret"}, "Odoo returned HTTP 500."),
        (500, "not-json-object", "Odoo returned HTTP 500."),
    ],
)
async def test_http_error_detail_is_safe_and_bounded(
    status_code: int,
    body: object,
    expected: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    client = OdooJson2Client(
        base_url="https://example.odoo.com",
        database="example",
        api_key="secret",
        timeout_seconds=10,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.odoo.com"),
    )

    with pytest.raises(ConnectorError) as exc_info:
        await client.create_account_move({"move_type": "in_invoice", "api_key": "secret"})

    assert exc_info.value.safe_message == expected
    assert "secret" not in exc_info.value.safe_message


async def test_validation_error_preserves_type_with_safe_detail() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "x_name is required."})

    client = OdooJson2Client(
        base_url="https://example.odoo.com",
        database="example",
        api_key="secret",
        timeout_seconds=10,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.odoo.com"),
    )

    with pytest.raises(ConnectorValidationError) as exc_info:
        await client.create_account_move({"move_type": "in_invoice"})

    assert exc_info.value.safe_message == "Odoo rejected the request payload. x_name is required."


async def test_search_read_uses_read_only_endpoint() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/json/2/res.partner/search_read"
        assert b"create" not in request.content
        assert b"write" not in request.content
        assert b'"offset":0' in request.content
        return httpx.Response(200, json=[{"id": 1, "name": "Supplier"}])

    client = OdooJson2Client(
        base_url="https://example.odoo.com",
        database="example",
        api_key="secret",
        timeout_seconds=10,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.odoo.com"),
    )

    assert await client.search_read(model="res.partner", domain=[], fields=["id", "name"]) == [
        {"id": 1, "name": "Supplier"}
    ]


async def test_search_read_allows_account_move_for_duplicate_detection() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/json/2/account.move/search_read"
        return httpx.Response(200, json=[])

    client = OdooJson2Client(
        base_url="https://example.odoo.com",
        database="example",
        api_key="secret",
        timeout_seconds=10,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.odoo.com"),
    )

    assert await client.search_read(model="account.move", domain=[], fields=["id"]) == []


async def test_search_read_rejects_non_allowlisted_model() -> None:
    client = OdooJson2Client(
        base_url="https://example.odoo.com",
        database="example",
        api_key="secret",
        timeout_seconds=10,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))),
    )

    with pytest.raises(ConnectorError):
        await client.search_read(model="account.payment", domain=[], fields=["id"])


async def test_create_studio_record_uses_configured_studio_model() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/json/2/x_ipp_import_workbench/create"
        assert json.loads(request.content) == {"vals_list": [{"x_studio_review_id": "review-1"}]}
        return httpx.Response(200, json=42)

    client = OdooJson2Client(
        base_url="https://example.odoo.com",
        database="example",
        api_key="secret",
        timeout_seconds=10,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.odoo.com"),
    )

    assert (
        await client.create_studio_record(
            model="x_ipp_import_workbench",
            values={"x_studio_review_id": "review-1"},
        )
        == 42
    )


async def test_create_studio_record_accepts_single_integer_id_list() -> None:
    client = OdooJson2Client(
        base_url="https://example.odoo.com",
        database="example",
        api_key="secret",
        timeout_seconds=10,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[123])),
            base_url="https://example.odoo.com",
        ),
    )

    assert await client.create_studio_record(model="x_ipp_import_workbench", values={"x_name": "review-1"}) == 123


@pytest.mark.parametrize(
    "response_body",
    [True, False, {"id": True}, {"id": False}, [], [1, 2], ["1"], [True], [{"id": 1}], {"ids": [1]}],
)
async def test_create_studio_record_rejects_unsupported_response_shapes(response_body: object) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body)

    client = OdooJson2Client(
        base_url="https://example.odoo.com",
        database="example",
        api_key="secret",
        timeout_seconds=10,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.odoo.com"),
    )

    with pytest.raises(ConnectorError):
        await client.create_studio_record(model="x_ipp_import_workbench", values={"x_name": "review-1"})


@pytest.mark.parametrize(
    ("response_body", "expected_shape", "forbidden_text"),
    [
        ([1, 2], "type=list,length=2,element_types=['int']", None),
        ({"ids": [1], "token": "secret-value"}, "type=dict,keys=['ids', 'token']", "secret-value"),
        ("secret-value", "type=str", "secret-value"),
    ],
)
async def test_create_studio_record_reports_safe_unexpected_response_shape(
    response_body: object,
    expected_shape: str,
    forbidden_text: str | None,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body)

    client = OdooJson2Client(
        base_url="https://example.odoo.com",
        database="example",
        api_key="secret",
        timeout_seconds=10,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.odoo.com"),
    )

    with pytest.raises(ConnectorError) as exc_info:
        await client.create_studio_record(model="x_ipp_import_workbench", values={"x_name": "review-1"})

    assert exc_info.value.safe_message == (
        f"Odoo Studio create returned an unexpected response shape: {expected_shape}."
    )
    if forbidden_text is not None:
        assert forbidden_text not in exc_info.value.safe_message


async def test_write_studio_record_uses_configured_studio_model_and_id_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/json/2/x_ipp_import_workbench/write"
        assert b'"ids":[42]' in request.content
        assert b'"values":{"x_studio_review_version":2}' in request.content
        return httpx.Response(200, json=True)

    client = OdooJson2Client(
        base_url="https://example.odoo.com",
        database="example",
        api_key="secret",
        timeout_seconds=10,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.odoo.com"),
    )

    assert await client.write_studio_record(
        model="x_ipp_import_workbench",
        record_id=42,
        values={"x_studio_review_version": 2},
    )


async def test_studio_writes_reject_non_studio_models() -> None:
    client = OdooJson2Client(
        base_url="https://example.odoo.com",
        database="example",
        api_key="secret",
        timeout_seconds=10,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=True))),
    )

    with pytest.raises(ConnectorError):
        await client.create_studio_record(model="account.move", values={"name": "unsafe"})
    with pytest.raises(ConnectorError):
        await client.write_studio_record(model="res.partner", record_id=1, values={"name": "unsafe"})
