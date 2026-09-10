from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class WorkbenchReviewSupplierResolution(Base):
    """Immutable record of one explicit supplier-resolution decision for a review version.

    It does NOT overwrite the original ``SUPPLIER_NOT_FOUND`` classification or the
    factual ``PartnerMatchResult``; those remain historically truthful. Legal
    supplier identity (name, VAT) is not stored here -- it lives in
    ``workbench_review_source_invoice_evidence``. Append-only: no UPDATE path, no
    DELETE in the normal workflow.
    """

    __tablename__ = "workbench_review_supplier_resolutions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_workbench_review_supplier_resolutions_review_id",
        ),
        CheckConstraint(
            "company_id > 0",
            name="ck_workbench_review_supplier_resolutions_company_id_positive",
        ),
        CheckConstraint(
            "review_version > 0",
            name="ck_workbench_review_supplier_resolutions_review_version_positive",
        ),
        CheckConstraint(
            "(mode = 'match_existing' AND resolved_partner_id IS NOT NULL) "
            "OR (mode <> 'match_existing' AND resolved_partner_id IS NULL)",
            name="ck_workbench_review_supplier_resolutions_partner_id_by_mode",
        ),
        CheckConstraint(
            "resolved_partner_id IS NULL OR resolved_partner_id > 0",
            name="ck_workbench_review_supplier_resolutions_partner_id_positive",
        ),
        UniqueConstraint(
            "review_id",
            "review_version",
            name="uq_workbench_review_supplier_resolutions_review_version",
        ),
        Index(
            "ix_workbench_review_supplier_resolutions_company_review",
            "company_id",
            "review_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    review_version: Mapped[int] = mapped_column(nullable=False)
    source_invoice_id: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    resolved_partner_id: Mapped[int | None] = mapped_column(nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    note: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
