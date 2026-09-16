from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime

ONE_OFF_VENDOR_RETIREMENT_STATUSES = (
    "pending_vendor_bill",
    "archive_attempted",
    "archived",
    "needs_reconciliation",
)


class WorkbenchReviewOneOffVendorRetirement(Base):
    """Durable state machine for one review's ONE_OFF_VENDOR archive lifecycle (P0-PROD-08H).

    One row per ``(review_id, company_id, review_version)``, created in the same
    commit as the review's ``SupplierRemediationEffect`` (mode=one_off_vendor).
    ``status`` advances strictly forward via UPDATE -- this row IS the
    crash-recovery state for the archive-last step, exactly as
    ``WorkbenchReviewProductRemediationReservation`` is for CREATE_NEW_PRODUCT.

    Constraint/index names are deliberately abbreviated ("wrov_retirements") and
    kept under PostgreSQL's 63-byte NAMEDATALEN limit -- see the identical
    convention (and the incident it prevents) documented on
    ``workbench_review_product_remediation_reservations``.
    """

    __tablename__ = "workbench_review_one_off_vendor_retirements"
    __table_args__ = (
        ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_wrov_retirements_review_id",
        ),
        CheckConstraint(
            "company_id > 0",
            name="ck_wrov_retirements_company_positive",
        ),
        CheckConstraint(
            "review_version > 0",
            name="ck_wrov_retirements_version_positive",
        ),
        CheckConstraint(
            "resolved_partner_id > 0",
            name="ck_wrov_retirements_partner_positive",
        ),
        CheckConstraint(
            "status IN ('pending_vendor_bill', 'archive_attempted', 'archived', 'needs_reconciliation')",
            name="ck_wrov_retirements_status",
        ),
        UniqueConstraint(
            "review_id",
            "company_id",
            "review_version",
            name="uq_wrov_retirements_review_version",
        ),
        Index(
            "ix_wrov_retirements_company_review",
            "company_id",
            "review_id",
        ),
        Index(
            "ix_wrov_retirements_partner_id",
            "resolved_partner_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    review_version: Mapped[int] = mapped_column(nullable=False)
    resolved_partner_id: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        AwareDateTime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )
