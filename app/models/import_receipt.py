from datetime import datetime

from sqlalchemy import Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class ImportReceipt(Base):
    """Technical idempotency receipt for canonical import acceptance."""

    __tablename__ = "import_receipts"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "idempotency_key",
            name="uq_import_receipts_company_idempotency_key",
        ),
        Index("ix_import_receipts_idempotency_key", "idempotency_key"),
        Index("ix_import_receipts_company_status", "company_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    invoice_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    vendor_bill_id: Mapped[int | None] = mapped_column(nullable=True)
    review_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
