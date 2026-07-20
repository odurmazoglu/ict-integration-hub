from fastapi import APIRouter

from app.schemas.normalized_invoice import NormalizedInvoice
from app.schemas.odoo_mapping import OdooMappingPreview
from app.services.odoo_mapping_preview import OdooMappingPreviewService

router = APIRouter(prefix="/api/v1/odoo", tags=["odoo-mapping"])


@router.post("/mapping-preview", response_model=OdooMappingPreview)
def preview_odoo_mapping(invoice: NormalizedInvoice) -> OdooMappingPreview:
    return OdooMappingPreviewService().build_preview(invoice)
