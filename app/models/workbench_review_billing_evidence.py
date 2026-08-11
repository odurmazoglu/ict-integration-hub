from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class WorkbenchReviewBillingEvidence(Base):
    __tablename__ = "workbench_review_billing_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_workbench_review_billing_evidence_review_id",
        ),
        CheckConstraint(
            "company_id > 0",
            name="ck_workbench_review_billing_evidence_company_id_positive",
        ),
        CheckConstraint(
            "review_version > 0",
            name="ck_workbench_review_billing_evidence_review_version_positive",
        ),
        CheckConstraint(
            "schema_version > 0",
            name="ck_workbench_review_billing_evidence_schema_version_positive",
        ),
        UniqueConstraint(
            "company_id",
            "review_id",
            "review_version",
            "billing_key",
            name="uq_workbench_review_billing_evidence_company_review_version_key",
        ),
        Index(
            "ix_workbench_review_billing_evidence_company_review_version",
            "company_id",
            "review_id",
            "review_version",
        ),
        Index("ix_workbench_review_billing_evidence_billing_key", "billing_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    review_version: Mapped[int] = mapped_column(nullable=False)
    billing_key: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[int] = mapped_column(nullable=False, default=1)
    billing_instruction: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
