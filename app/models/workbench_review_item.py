from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Date, Index, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime

REVIEW_AMOUNT_PRECISION = 24
REVIEW_AMOUNT_SCALE = 6


class WorkbenchReviewItem(Base):
    __tablename__ = "workbench_review_items"
    __table_args__ = (
        UniqueConstraint("review_id", name="uq_workbench_review_items_review_id"),
        UniqueConstraint(
            "company_id",
            "idempotency_key",
            name="uq_workbench_review_items_company_idempotency_key",
        ),
        Index("ix_workbench_review_items_company_id", "company_id"),
        Index("ix_workbench_review_items_status", "status"),
        Index("ix_workbench_review_items_created_at", "created_at"),
        Index("ix_workbench_review_items_supplier_tax_number", "supplier_tax_number"),
        Index(
            "ix_workbench_review_items_company_status_created",
            "company_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    invoice_id: Mapped[str] = mapped_column(String(255), nullable=False)
    invoice_number: Mapped[str | None] = mapped_column(String(255), nullable=True)
    supplier_tax_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supplier_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    invoice_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    total_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(REVIEW_AMOUNT_PRECISION, REVIEW_AMOUNT_SCALE),
        nullable=True,
    )
    workflow: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    review_reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        AwareDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
