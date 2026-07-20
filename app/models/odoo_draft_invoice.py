from datetime import datetime

from sqlalchemy import Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class OdooDraftInvoice(Base):
    __tablename__ = "odoo_draft_invoices"
    __table_args__ = (
        UniqueConstraint("ettn", name="uq_odoo_draft_invoice_ettn"),
        Index("ix_odoo_draft_invoices_odoo_move_id", "odoo_move_id"),
        Index("ix_odoo_draft_invoices_creation_status", "creation_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    integration_invoice_id: Mapped[int | None] = mapped_column(nullable=True)
    ettn: Mapped[str] = mapped_column(String(255), nullable=False)
    invoice_number: Mapped[str | None] = mapped_column(String(255), nullable=True)
    odoo_model: Mapped[str] = mapped_column(String(64), nullable=False, default="account.move")
    odoo_move_id: Mapped[int | None] = mapped_column(nullable=True)
    creation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    safe_error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    safe_error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), nullable=True)
    odoo_created_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        AwareDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
