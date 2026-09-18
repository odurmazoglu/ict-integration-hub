from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime

WRITE_AUTHORIZATION_STATUSES = (
    "pending",
    "consumed",
    "revoked",
)

WRITE_AUTHORIZATION_OPERATION_TYPES = ("EXECUTE_VENDOR_BILL",)


class WorkbenchReviewWriteAuthorization(Base):
    """Durable, auditable, short-lived write authorization for operator-driven execution.

    One row per issued authorization, scoped strictly to:
    (company_id, review_id, operation_type, target_version).

    Consumption and runtime outcome share the application transaction. A crash
    before its commit rolls back consumption; recovery stays bound to one execution.

    Transitions from 'pending' to 'consumed' atomically using SELECT FOR UPDATE
    prior to remote ERP write.
    """

    __tablename__ = "workbench_review_write_authorizations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_wr_write_auth_review_id",
        ),
        UniqueConstraint("authorization_id", name="uq_wr_write_auth_id"),
        CheckConstraint("company_id > 0", name="ck_wr_write_auth_company_pos"),
        CheckConstraint("target_version > 0", name="ck_wr_write_auth_version_pos"),
        CheckConstraint(
            "operation_type IN ('EXECUTE_VENDOR_BILL')",
            name="ck_wr_write_auth_op_type",
        ),
        CheckConstraint("use_count >= 0", name="ck_wr_write_auth_use_count"),
        CheckConstraint(
            "status != 'consumed' OR (consumed_at IS NOT NULL "
            "AND consumed_by_execution_id IS NOT NULL AND use_count > 0)",
            name="ck_wr_write_auth_consumed",
        ),
        CheckConstraint(
            "status IN ('pending', 'consumed', 'revoked')",
            name="ck_wr_write_auth_status",
        ),
        Index(
            "ix_wr_write_auth_lookup",
            "company_id",
            "review_id",
            "operation_type",
            "target_version",
        ),
        Index(
            "ix_wr_write_auth_status",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    authorization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_version: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    authorized_by: Mapped[str] = mapped_column(String(255), nullable=False)
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), nullable=True)
    consumed_by_trace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    consumed_by_execution_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_used_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), nullable=True)
    last_used_trace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
