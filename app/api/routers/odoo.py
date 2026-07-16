from fastapi import APIRouter

from app.api.dependencies import OdooClientDep
from app.schemas.odoo import OdooProbeResponse

router = APIRouter(prefix="/api/v1/connectors/odoo", tags=["odoo"])


@router.get("/probe", response_model=OdooProbeResponse)
async def probe_odoo(client: OdooClientDep) -> OdooProbeResponse:
    return await client.probe()
