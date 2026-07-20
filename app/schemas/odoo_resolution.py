from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.odoo_mapping import OdooMappingPreview

ResolutionStatus = Literal["resolved", "unresolved", "ambiguous", "invalid", "not_required"]
OverallResolutionStatus = Literal["resolved", "needs_review", "invalid"]
ResolutionEntityType = Literal["partner", "product", "tax", "currency", "journal"]


class OdooResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview: OdooMappingPreview
    company_id: int | None = None
    purchase_journal_id: int | None = None
    purchase_journal_code: str | None = None
    allow_productless_lines: bool = False


class OdooResolutionIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    field_path: str
    entity_type: ResolutionEntityType


class OdooEntityResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: ResolutionEntityType
    status: ResolutionStatus
    odoo_id: int | None = None
    match_method: str | None = None
    candidate_count: int
    field_path: str
    safe_message: str | None = None


class OdooLineResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int
    product: OdooEntityResolution
    taxes: list[OdooEntityResolution]


class OdooResolutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution_status: OverallResolutionStatus
    reviewed_preview: OdooMappingPreview
    partner: OdooEntityResolution
    currency: OdooEntityResolution
    journal: OdooEntityResolution
    lines: list[OdooLineResolution]
    warnings: list[OdooResolutionIssue] = Field(default_factory=list)
    missing_matches: list[OdooResolutionIssue] = Field(default_factory=list)
    ambiguous_matches: list[OdooResolutionIssue] = Field(default_factory=list)
