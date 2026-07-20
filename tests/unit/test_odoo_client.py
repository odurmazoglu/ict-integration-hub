import httpx
import pytest

from app.connectors.exceptions import ConnectorError
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
