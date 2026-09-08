from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class WorkbenchReviewReclassification(Base):
    """Immutable record of one non-destructive deterministic reclassification.

    "Review version ``from_version`` was deterministically reclassified into
    ``to_version``." It preserves the previous workflow/reasons so history proves
    what version N was, why the reclassification ran, and what deterministic
    result produced version N+1. The full source invoice is NOT stored here --
    it already lives immutably in ``workbench_review_source_invoice_evidence``.
    Insert-only: no UPDATE path, no DELETE in the normal workflow.
    """

    __tablename__ = "workbench_review_reclassifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_workbench_review_reclassifications_review_id",
        ),
        CheckConstraint(
            "company_id > 0",
            name="ck_workbench_review_reclassifications_company_id_positive",
        ),
        CheckConstraint(
            "from_version > 0",
            name="ck_workbench_review_reclassifications_from_version_positive",
        ),
        CheckConstraint(
            "to_version > 0",
            name="ck_workbench_review_reclassifications_to_version_positive",
        ),
        CheckConstraint(
            "to_version = from_version + 1",
            name="ck_workbench_review_reclassifications_single_step",
        ),
        UniqueConstraint(
            "review_id",
            "to_version",
            name="uq_workbench_review_reclassifications_review_to_version",
        ),
        UniqueConstraint(
            "review_id",
            "from_version",
            name="uq_workbench_review_reclassifications_review_from_version",
        ),
        Index(
            "ix_workbench_review_reclassifications_company_review",
            "company_id",
            "review_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    from_version: Mapped[int] = mapped_column(nullable=False)
    to_version: Mapped[int] = mapped_column(nullable=False)
    source_invoice_id: Mapped[str] = mapped_column(String(255), nullable=False)
    trigger: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    previous_workflow: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_review_reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    new_workflow: Mapped[str] = mapped_column(String(64), nullable=False)
    new_review_reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    matched_rule_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    matched_rule_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    executable: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
