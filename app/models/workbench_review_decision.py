from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class WorkbenchReviewDecision(Base):
    __tablename__ = "workbench_review_decisions"
    __table_args__ = (
        UniqueConstraint("decision_id", name="uq_workbench_review_decisions_decision_id"),
        UniqueConstraint(
            "company_id",
            "idempotency_key",
            name="uq_workbench_review_decisions_company_idempotency_key",
        ),
        ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_workbench_review_decisions_review_id",
        ),
        CheckConstraint("review_version_before > 0", name="ck_workbench_review_decisions_version_before_positive"),
        CheckConstraint("review_version_after > 0", name="ck_workbench_review_decisions_version_after_positive"),
        CheckConstraint(
            "review_version_after = review_version_before + 1",
            name="ck_workbench_review_decisions_version_increment",
        ),
        Index("ix_workbench_review_decisions_company_id", "company_id"),
        Index("ix_workbench_review_decisions_review_id", "review_id"),
        Index("ix_workbench_review_decisions_decision_type", "decision_type"),
        Index("ix_workbench_review_decisions_submitted_at", "submitted_at"),
        Index("ix_workbench_review_decisions_company_review", "company_id", "review_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(255), nullable=False)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    review_version_before: Mapped[int] = mapped_column(nullable=False)
    review_version_after: Mapped[int] = mapped_column(nullable=False)
    decision_type: Mapped[str] = mapped_column(String(64), nullable=False)
    selected_workflow: Mapped[str | None] = mapped_column(String(64), nullable=True)
    selected_partner_id: Mapped[int | None] = mapped_column(nullable=True)
    line_resolutions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    tax_resolutions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    business_context: Mapped[dict[str, int] | None] = mapped_column(JSON, nullable=True)
    business_context_allocations: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    decided_by: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
