from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKeyConstraint, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class ExecutionSourceInvoiceEvidence(Base):
    __tablename__ = "execution_source_invoice_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["decision_id"],
            ["workbench_review_decisions.decision_id"],
            name="fk_execution_source_invoice_evidence_decision_id",
        ),
        CheckConstraint("company_id > 0", name="ck_execution_source_invoice_evidence_company_id_positive"),
        CheckConstraint(
            "decision_version > 0",
            name="ck_execution_source_invoice_evidence_decision_version_positive",
        ),
        Index(
            "ix_execution_source_invoice_evidence_company_review_version",
            "company_id",
            "review_id",
            "decision_version",
        ),
        Index("ix_execution_source_invoice_evidence_decision_id", "decision_id"),
        Index("ix_execution_source_invoice_evidence_source_invoice_id", "source_invoice_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    decision_version: Mapped[int] = mapped_column(nullable=False)
    decision_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_invoice_id: Mapped[str] = mapped_column(String(255), nullable=False)
    invoice: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    partner_match: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    product_match: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    tax_match: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
