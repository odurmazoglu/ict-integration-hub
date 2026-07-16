from httpx import AsyncClient

from app.api.dependencies import get_uyumsoft_client
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.core.config import Settings, get_settings
from app.main import app
from app.schemas.uyumsoft import UyumsoftOperationsResponse


class FakeUyumsoftClient(UyumsoftSoapClient):
    def __init__(self) -> None:
        pass

    def inspect_wsdl(self) -> UyumsoftOperationsResponse:
        return UyumsoftOperationsResponse(
            status="ok",
            wsdl_url="https://uyumsoft.test/wsdl",
            operations=["SendInvoice", "TestConnection"],
            read_only_operations=["TestConnection"],
        )


async def test_operations_endpoint_is_mockable(api_client: AsyncClient) -> None:
    app.dependency_overrides[get_uyumsoft_client] = lambda: FakeUyumsoftClient()
    try:
        response = await api_client.get("/api/v1/connectors/uyumsoft/operations")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["read_only_operations"] == ["TestConnection"]


async def test_operations_endpoint_returns_404_outside_development(api_client: AsyncClient) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(app_env="production")
    app.dependency_overrides[get_uyumsoft_client] = lambda: FakeUyumsoftClient()
    try:
        response = await api_client.get("/api/v1/connectors/uyumsoft/operations")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
