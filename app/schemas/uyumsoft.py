from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class UyumsoftTestConnectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    result: str


class UyumsoftIdentityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    identity: dict[str, Any]


class UyumsoftSystemDateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    system_date: datetime


class UyumsoftOperationsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    wsdl_url: str
    operations: list[str]
    read_only_operations: list[str]

