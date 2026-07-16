import json

import httpx
import pytest

from app.connectors.odoo.client import OdooJson2Client


@pytest.mark.asyncio
async def test_odoo_probe_uses_read_only_search_read() -> None:
    captured_payload: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        assert request.url.path == "/json/2/res.company/search_read"
        assert request.headers["authorization"] == "bearer secret"
        assert request.headers["x-odoo-database"] == "demo"
        return httpx.Response(200, json=[{"id": 7, "name": "Demo Company"}])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://odoo.test") as http_client:
        client = OdooJson2Client(
            base_url="https://odoo.test",
            database="demo",
            api_key="secret",
            timeout_seconds=1,
            http_client=http_client,
        )

        result = await client.probe()

    assert result.company_id == 7
    assert result.company_name == "Demo Company"
    assert captured_payload == {"domain": [], "fields": ["id", "name"], "limit": 1}
