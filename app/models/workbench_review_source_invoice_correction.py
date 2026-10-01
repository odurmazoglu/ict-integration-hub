from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class WorkbenchReviewSourceInvoiceCorrection(Base):
    """Immutable, audited correction of one field of a review's source-invoice snapshot.

    The original ``workbench_review_source_invoice_evidence`` row is never modified.
    The effective source invoice is that original with every correction of the review
    overlaid in ``to_version`` order (see ``SqlAlchemyReviewSourceInvoiceEvidenceReader``).
    Each correction is applied atomically with a single ``from_version`` ->
    ``to_version`` review version advance and a ``source_identity_corrected``
    reclassification event.

    Append-only: no UPDATE path, no DELETE in the normal workflow.
    """

    __tablename__ = "workbench_review_source_invoice_corrections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_wr_source_corrections_review_id",
        ),
        CheckConstraint(
            "company_id > 0",
            name="ck_wr_source_corrections_company_id_positive",
        ),
        CheckConstraint(
            "from_version > 0",
            name="ck_wr_source_corrections_from_version_positive",
        ),
        CheckConstraint(
            "to_version = from_version + 1",
            name="ck_wr_source_corrections_single_step",
        ),
        CheckConstraint(
            "field_path IN ('supplier.tax_number')",
            name="ck_wr_source_corrections_field_path",
        ),
        CheckConstraint(
            "reason IN ('UBL_PARTY_TAX_IDENTIFIER_PR201')",
            name="ck_wr_source_corrections_reason",
        ),
        CheckConstraint(
            "old_value IS NULL OR old_value <> new_value",
            name="ck_wr_source_corrections_value_changes",
        ),
        CheckConstraint(
            "source_document_id > 0",
            name="ck_wr_source_corrections_document_id_positive",
        ),
        UniqueConstraint(
            "review_id",
            "to_version",
            name="uq_wr_source_corrections_review_to_version",
        ),
        UniqueConstraint(
            "review_id",
            "from_version",
            name="uq_wr_source_corrections_review_from_version",
        ),
        Index(
            "ix_wr_source_corrections_company_review",
            "company_id",
            "review_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    from_version: Mapped[int] = mapped_column(nullable=False)
    to_version: Mapped[int] = mapped_column(nullable=False)
    source_invoice_id: Mapped[str] = mapped_column(String(255), nullable=False)
    field_path: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    new_value: Mapped[str] = mapped_column(String(255), nullable=False)
    source_document_id: Mapped[int] = mapped_column(nullable=False)
    source_document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
