from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime

OPERATOR_REQUEST_STATUSES = ("in_progress", "completed", "stale", "rejected", "unauthorized", "failed")
#: Must equal ``OperatorRequestAction`` values (guarded by a regression test); the CHECK
#: constraint below is generated from this tuple. Migration 202607170038 added
#: ``product_mapping``; 202607170039 added ``product_line_mapping`` (PR C), whose
#: ``odoo_record_id`` is a Workbench *child* product line row, not a parent row.
OPERATOR_REQUEST_ACTIONS = (
    "supplier_resolution",
    "purchase_purpose",
    "accounting_resolution",
    "decision",
    "execute_vendor_bill",
    "product_mapping",
    "product_line_mapping",
)


class WorkbenchOperatorRequest(Base):
    """Hub ledger of Odoo Workbench operator requests (ADR-0013).

    One row per distinct request (``request_key`` = deterministic hash of the typed
    Odoo request). It is audit and idempotency state only: authoritative business
    state lives in the existing resolution/decision/execution tables, written by the
    existing use cases. A terminal row means "already handled -- only re-acknowledge";
    ``authorization_id`` records the narrow write authorization issued for this request
    so a resumed request never issues a second one.
    """

    __tablename__ = "workbench_operator_requests"
    __table_args__ = (
        UniqueConstraint("request_key", name="uq_workbench_operator_requests_request_key"),
        CheckConstraint("company_id > 0", name="ck_workbench_operator_requests_company_pos"),
        CheckConstraint("odoo_record_id > 0", name="ck_workbench_operator_requests_odoo_record_pos"),
        CheckConstraint("expected_version > 0", name="ck_workbench_operator_requests_version_pos"),
        CheckConstraint("requested_by_odoo_user_id > 0", name="ck_workbench_operator_requests_requester_pos"),
        CheckConstraint("attempts >= 0", name="ck_workbench_operator_requests_attempts"),
        CheckConstraint(
            "status IN ('in_progress', 'completed', 'stale', 'rejected', 'unauthorized', 'failed')",
            name="ck_workbench_operator_requests_status",
        ),
        CheckConstraint(
            "action IN (" + ", ".join(f"'{action}'" for action in OPERATOR_REQUEST_ACTIONS) + ")",
            name="ck_workbench_operator_requests_action",
        ),
        Index("ix_workbench_operator_requests_review", "company_id", "review_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    request_key: Mapped[str] = mapped_column(String(128), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    odoo_record_id: Mapped[int] = mapped_column(nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_version: Mapped[int] = mapped_column(nullable=False)
    requested_by_odoo_user_id: Mapped[int] = mapped_column(nullable=False)
    requested_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    authorization_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        AwareDateTime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )
