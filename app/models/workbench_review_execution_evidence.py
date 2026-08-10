from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class WorkbenchReviewExecutionEvidence(Base):
    __tablename__ = "workbench_review_execution_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_workbench_review_execution_evidence_review_id",
        ),
        CheckConstraint(
            "company_id > 0",
            name="ck_workbench_review_execution_evidence_company_id_positive",
        ),
        CheckConstraint(
            "review_version > 0",
            name="ck_workbench_review_execution_evidence_review_version_positive",
        ),
        CheckConstraint(
            "schema_version > 0",
            name="ck_workbench_review_execution_evidence_schema_version_positive",
        ),
        UniqueConstraint(
            "company_id",
            "review_id",
            "review_version",
            name="uq_workbench_review_execution_evidence_company_review_version",
        ),
        Index(
            "ix_workbench_review_execution_evidence_company_review_version",
            "company_id",
            "review_id",
            "review_version",
        ),
        Index("ix_workbench_review_execution_evidence_source_invoice_id", "source_invoice_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    review_version: Mapped[int] = mapped_column(nullable=False)
    source_invoice_id: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[int] = mapped_column(nullable=False, default=1)
    invoice: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    partner_match: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    product_match: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    tax_match: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
