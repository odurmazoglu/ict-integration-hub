from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class ExecutionCustomerBillingEvidence(Base):
    __tablename__ = "execution_customer_billing_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["decision_id"],
            ["workbench_review_decisions.decision_id"],
            name="fk_execution_customer_billing_evidence_decision_id",
        ),
        UniqueConstraint(
            "decision_id",
            "billing_key",
            name="uq_execution_customer_billing_evidence_decision_key",
        ),
        CheckConstraint("company_id > 0", name="ck_exec_cust_bill_ev_company_positive"),
        CheckConstraint(
            "decision_version > 0",
            name="ck_exec_cust_bill_ev_decision_version_positive",
        ),
        CheckConstraint("schema_version > 0", name="ck_exec_cust_bill_ev_schema_version_positive"),
        Index(
            "ix_execution_customer_billing_evidence_company_review_version",
            "company_id",
            "review_id",
            "decision_version",
        ),
        Index("ix_execution_customer_billing_evidence_decision_id", "decision_id"),
        Index("ix_execution_customer_billing_evidence_billing_key", "billing_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(255), nullable=False)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    decision_version: Mapped[int] = mapped_column(nullable=False)
    billing_key: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[int] = mapped_column(nullable=False, default=1)
    billing_instruction: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
