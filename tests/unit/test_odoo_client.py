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
