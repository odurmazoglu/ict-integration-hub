from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.odoo_mapping import OdooMappingPreview

OdooDraftCreationStatus = Literal["created", "existing", "failed"]


class OdooDraftInvoiceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview: OdooMappingPreview
    integration_invoice_id: int | None = None
    confirm_create_draft: bool = False


class OdooDraftInvoiceCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    integration_invoice_id: int | None = None
    ettn: str
    odoo_model: Literal["account.move"] = "account.move"
    odoo_move_id: int | None = None
    creation_status: OdooDraftCreationStatus
    safe_message: str | None = None
    created_at: datetime | None = None
