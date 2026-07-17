from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Index, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class UyumsoftInvoiceMetadata(Base):
    __tablename__ = "uyumsoft_invoice_metadata"
    __table_args__ = (
        UniqueConstraint("provider", "direction", "ettn", name="uq_uyumsoft_invoice_provider_direction_ettn"),
        UniqueConstraint(
            "provider",
            "direction",
            "identity_key",
            name="uq_uyumsoft_invoice_provider_direction_identity",
        ),
        Index("ix_uyumsoft_invoice_provider_direction", "provider", "direction"),
        Index("ix_uyumsoft_invoice_ettn", "ettn"),
        Index("ix_uyumsoft_invoice_invoice_date", "invoice_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_invoice_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ettn: Mapped[str | None] = mapped_column(String(255), nullable=True)
    identity_key: Mapped[str] = mapped_column(String(255), nullable=False)
    identity_strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    invoice_number: Mapped[str | None] = mapped_column(String(255), nullable=True)
    invoice_date: Mapped[datetime | None] = mapped_column(AwareDateTime(), nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sender_tax_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    receiver_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    receiver_tax_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    provider_status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        AwareDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
