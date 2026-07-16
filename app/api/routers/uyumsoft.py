from fastapi import APIRouter, HTTPException, status

from app.api.dependencies import SettingsDep, UyumsoftClientDep
from app.schemas.uyumsoft import (
    UyumsoftIdentityResponse,
    UyumsoftOperationsResponse,
    UyumsoftSystemDateResponse,
    UyumsoftTestConnectionResponse,
)

router = APIRouter(prefix="/api/v1/connectors/uyumsoft", tags=["uyumsoft"])


@router.get("/test-connection", response_model=UyumsoftTestConnectionResponse)
def test_connection(client: UyumsoftClientDep) -> UyumsoftTestConnectionResponse:
    return client.test_connection()


@router.get("/identity", response_model=UyumsoftIdentityResponse)
def identity(client: UyumsoftClientDep) -> UyumsoftIdentityResponse:
    return client.who_am_i()


@router.get("/system-date", response_model=UyumsoftSystemDateResponse)
def system_date(client: UyumsoftClientDep) -> UyumsoftSystemDateResponse:
    return client.get_system_date()


@router.get("/operations", response_model=UyumsoftOperationsResponse)
def operations(
    settings: SettingsDep,
    client: UyumsoftClientDep,
) -> UyumsoftOperationsResponse:
    if not settings.is_development:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="WSDL inspection is available only in development.",
        )
    return client.inspect_wsdl()
