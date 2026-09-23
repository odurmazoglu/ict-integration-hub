from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class WorkbenchReviewPurchasePurposeResolution(Base):
    """Immutable, review-scoped record of *why* a purchase was made (P0-PROD-15T).

    Never an accounting decision -- see ``WorkbenchReviewAccountingResolution`` for
    that. Never touches the supplier-wide ``operating_expense_mappings`` table: a
    supplier can be mixed-purpose, so purpose is recorded per review, not per
    supplier.

    Append-only: no UPDATE path, no DELETE in the normal workflow.
    """

    __tablename__ = "workbench_review_purchase_purpose_resolutions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_workbench_review_purchase_purpose_resolutions_review_id",
        ),
        CheckConstraint(
            "company_id > 0",
            name="ck_workbench_review_purchase_purpose_resolutions_company_id_positive",
        ),
        CheckConstraint(
            "review_version > 0",
            name="ck_workbench_review_purchase_purpose_resolutions_review_version_positive",
        ),
        CheckConstraint(
            "purchase_purpose IN ('internal_use', 'resale', 'customer_project', 'other_operating_expense')",
            name="ck_workbench_review_purchase_purpose_resolutions_purpose",
        ),
        UniqueConstraint(
            "review_id",
            "review_version",
            name="uq_workbench_review_purchase_purpose_resolutions_review_version",
        ),
        Index(
            "ix_workbench_review_purchase_purpose_resolutions_company_review",
            "company_id",
            "review_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    review_version: Mapped[int] = mapped_column(nullable=False)
    source_invoice_id: Mapped[str] = mapped_column(String(255), nullable=False)
    purchase_purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    note: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
