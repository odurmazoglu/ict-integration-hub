from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime

PRODUCT_RESERVATION_STATUSES = (
    "reserved",
    "create_attempted",
    "product_created",
    "completed",
    "reused_existing_product",
    "needs_reconciliation",
)


class WorkbenchReviewProductRemediationReservation(Base):
    """Durable state machine for one review-line CREATE_NEW_PRODUCT reservation (P0-PROD-07G).

    One row per ``(review_id, company_id, review_version, line_number)`` -- the
    review-line ownership identity. Reserved BEFORE any Odoo write; ``status`` and
    the Odoo identity columns advance strictly forward via UPDATE (unlike the
    append-only supplier-remediation tables, this row IS the crash-recovery state).
    """

    __tablename__ = "workbench_review_product_remediation_reservations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_workbench_review_product_remediation_reservations_review_id",
        ),
        CheckConstraint(
            "company_id > 0",
            name="ck_workbench_review_product_remediation_reservations_company_id_positive",
        ),
        CheckConstraint(
            "review_version > 0",
            name="ck_workbench_review_product_remediation_reservations_review_version_positive",
        ),
        CheckConstraint(
            "resolved_supplier_partner_id > 0",
            name="ck_workbench_review_product_remediation_reservations_partner_id_positive",
        ),
        CheckConstraint(
            "status IN ('reserved', 'create_attempted', 'product_created', 'completed', "
            "'reused_existing_product', 'needs_reconciliation')",
            name="ck_workbench_review_product_remediation_reservations_status",
        ),
        CheckConstraint(
            "product_template_id IS NULL OR product_template_id > 0",
            name="ck_workbench_review_product_remediation_reservations_template_id_positive",
        ),
        CheckConstraint(
            "product_id IS NULL OR product_id > 0",
            name="ck_workbench_review_product_remediation_reservations_product_id_positive",
        ),
        CheckConstraint(
            "supplierinfo_id IS NULL OR supplierinfo_id > 0",
            name="ck_workbench_review_product_remediation_reservations_supplierinfo_id_positive",
        ),
        CheckConstraint(
            "status IN ('reserved', 'create_attempted', 'needs_reconciliation') OR product_template_id IS NOT NULL",
            name="ck_workbench_review_product_remediation_reservations_template_by_status",
        ),
        CheckConstraint(
            "status != 'completed' OR supplierinfo_id IS NOT NULL",
            name="ck_workbench_review_product_remediation_reservations_supplierinfo_by_status",
        ),
        UniqueConstraint(
            "review_id",
            "company_id",
            "review_version",
            "line_number",
            name="uq_workbench_review_product_remediation_reservations_line",
        ),
        Index(
            "ix_workbench_review_product_remediation_reservations_company_review",
            "company_id",
            "review_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    review_version: Mapped[int] = mapped_column(nullable=False)
    line_number: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    resolved_supplier_partner_id: Mapped[int] = mapped_column(nullable=False)
    seller_item_code: Mapped[str] = mapped_column(String(255), nullable=False)
    product_name: Mapped[str] = mapped_column(String(512), nullable=False)
    is_storable: Mapped[bool] = mapped_column(nullable=False, default=False)
    internal_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    note: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_template_id: Mapped[int | None] = mapped_column(nullable=True)
    product_id: Mapped[int | None] = mapped_column(nullable=True)
    supplierinfo_id: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        AwareDateTime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )
