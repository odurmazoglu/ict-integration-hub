from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DocumentType = Literal["UBL_XML"]
DocumentDownloadStatus = Literal["downloaded", "existing"]


class DocumentDownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_ids: list[int] = Field(min_length=1, max_length=20)
    document_type: DocumentType = "UBL_XML"
    confirm_read_only: bool = False


class DocumentDownloadItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_id: int
    document_id: int
    status: DocumentDownloadStatus
    document_type: DocumentType
    storage_backend: str
    storage_key: str
    content_hash_sha256: str
    content_size_bytes: int


class DocumentDownloadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    document_type: DocumentType
    downloaded: int
    existing: int
    items: list[DocumentDownloadItemResponse]
