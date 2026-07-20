from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class InvoiceDocument(Base):
    __tablename__ = "invoice_documents"
    __table_args__ = (
        UniqueConstraint("invoice_id", "document_type", name="uq_invoice_document_invoice_type"),
        UniqueConstraint("storage_backend", "storage_key", name="uq_invoice_document_storage_key"),
        Index("ix_invoice_documents_provider_direction", "provider", "direction"),
        Index("ix_invoice_documents_content_hash", "content_hash_sha256"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("uyumsoft_invoice_metadata.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    document_type: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_hash_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    content_size_bytes: Mapped[int] = mapped_column(nullable=False)
    downloaded_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
