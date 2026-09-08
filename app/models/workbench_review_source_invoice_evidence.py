from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class WorkbenchReviewSourceInvoiceEvidence(Base):
    """Immutable canonical source-invoice snapshot: exactly one row per Workbench review.

    Stores only the serialized ``InternalInvoice`` the Hub classified. No match or
    workflow-decision facts (those live in ``workbench_review_execution_evidence``).
    Insert-only: no UPDATE path, no DELETE in normal workflow.
    """

    __tablename__ = "workbench_review_source_invoice_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_workbench_review_source_invoice_evidence_review_id",
        ),
        UniqueConstraint(
            "review_id",
            name="uq_workbench_review_source_invoice_evidence_review_id",
        ),
        CheckConstraint(
            "company_id > 0",
            name="ck_workbench_review_source_invoice_evidence_company_id_positive",
        ),
        CheckConstraint(
            "review_version > 0",
            name="ck_workbench_review_source_invoice_evidence_review_version_positive",
        ),
        CheckConstraint(
            "schema_version > 0",
            name="ck_workbench_review_source_invoice_evidence_schema_version_positive",
        ),
        Index(
            "ix_workbench_review_source_invoice_evidence_company_review",
            "company_id",
            "review_id",
        ),
        Index(
            "ix_workbench_review_source_invoice_evidence_source_invoice_id",
            "source_invoice_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    review_version: Mapped[int] = mapped_column(nullable=False)
    source_invoice_id: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[int] = mapped_column(nullable=False, default=1)
    invoice: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
