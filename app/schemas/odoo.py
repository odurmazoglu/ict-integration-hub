from pydantic import BaseModel, ConfigDict


class OdooProbeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    company_id: int
    company_name: str

