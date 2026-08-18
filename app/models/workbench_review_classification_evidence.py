from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class WorkbenchReviewClassificationEvidence(Base):
    __tablename__ = "workbench_review_classification_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_wbrce_review_id",
        ),
        CheckConstraint(
            "company_id > 0",
            name="ck_wbrce_company_id_positive",
        ),
        CheckConstraint(
            "review_version > 0",
            name="ck_wbrce_review_version_positive",
        ),
        CheckConstraint(
            "schema_version > 0",
            name="ck_wbrce_schema_version_positive",
        ),
        UniqueConstraint(
            "company_id",
            "review_id",
            "review_version",
            name="uq_wbrce_company_review_version",
        ),
        Index(
            "ix_wbrce_company_review_version",
            "company_id",
            "review_id",
            "review_version",
        ),
        Index("ix_wbrce_status", "status"),
        Index("ix_wbrce_rule_code", "matched_rule_code"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    review_version: Mapped[int] = mapped_column(nullable=False)
    schema_version: Mapped[int] = mapped_column(nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    matched_rule_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    matched_rule_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    matched_rule_version: Mapped[int | None] = mapped_column(nullable=True)
    matched_rule_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    workflow: Mapped[str | None] = mapped_column(String(64), nullable=True)
    classification_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    require_review: Mapped[bool] = mapped_column(nullable=False, default=False)
    require_business_context: Mapped[bool] = mapped_column(nullable=False, default=False)
    conflicting_rules: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
