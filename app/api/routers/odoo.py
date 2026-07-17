from fastapi import APIRouter, HTTPException, status

from app.api.dependencies import OdooClientDep
from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.schemas.odoo import OdooProbeResponse

router = APIRouter(prefix="/api/v1/connectors/odoo", tags=["odoo"])


@router.get("/probe", response_model=OdooProbeResponse)
async def probe_odoo(client: OdooClientDep) -> OdooProbeResponse:
    try:
        return await client.probe()
    except ConnectorTimeoutError as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=exc.safe_message) from exc
    except ConnectorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.safe_message) from exc
