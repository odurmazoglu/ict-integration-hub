from fastapi import APIRouter, HTTPException, status

from app.api.dependencies import SettingsDep, UyumsoftClientDep
from app.connectors.exceptions import ConnectorError
from app.schemas.uyumsoft import (
    UyumsoftIdentityResponse,
    UyumsoftOperationsResponse,
    UyumsoftSystemDateResponse,
    UyumsoftTestConnectionResponse,
)

router = APIRouter(prefix="/api/v1/connectors/uyumsoft", tags=["uyumsoft"])


@router.get("/test-connection", response_model=UyumsoftTestConnectionResponse)
def test_connection(client: UyumsoftClientDep) -> UyumsoftTestConnectionResponse:
    try:
        return client.test_connection()
    except ConnectorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.safe_message) from exc


@router.get("/identity", response_model=UyumsoftIdentityResponse)
def identity(client: UyumsoftClientDep) -> UyumsoftIdentityResponse:
    try:
        return client.who_am_i()
    except ConnectorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.safe_message) from exc


@router.get("/system-date", response_model=UyumsoftSystemDateResponse)
def system_date(client: UyumsoftClientDep) -> UyumsoftSystemDateResponse:
    try:
        return client.get_system_date()
    except ConnectorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.safe_message) from exc


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
    try:
        return client.inspect_wsdl()
    except ConnectorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.safe_message) from exc
