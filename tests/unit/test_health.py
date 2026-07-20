import pytest
from httpx import AsyncClient

from app.api.dependencies import get_settings
from app.core.config import Settings
from app.main import app


@pytest.mark.asyncio
async def test_health(api_client: AsyncClient) -> None:
    response = await api_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_liveness(api_client: AsyncClient) -> None:
    response = await api_client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readiness_checks_configuration_database_and_storage(
    api_client: AsyncClient,
    tmp_path,
) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url=f"sqlite:///{tmp_path / 'ready.db'}",
        document_storage_root=tmp_path / "documents",
    )
    try:
        response = await api_client.get("/health/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert {check["name"] for check in body["checks"]} == {"configuration", "database", "document_storage"}


@pytest.mark.asyncio
async def test_readiness_reports_safe_configuration_errors(api_client: AsyncClient, tmp_path) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="production",
        database_url="postgresql+psycopg://user:super-secret@localhost:5432/prod",
        document_storage_root=tmp_path / "documents",
        odoo_base_url="https://example.odoo.com",
        odoo_database="example",
        odoo_api_key="super-secret-api-key",
        uyumsoft_environment="test",
        uyumsoft_username="super-secret-user",
        uyumsoft_password="super-secret-password",
    )
    try:
        response = await api_client.get("/health/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    body = str(response.json())
    assert "super-secret" not in body
    assert "localhost" in body
