from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class WorkbenchReviewSupplierRemediationEffect(Base):
    """Immutable record of the *completed effect* of a supplier remediation.

    The operator's chosen *intent* is the ``workbench_review_supplier_resolutions``
    row (P0-3D2C). This row records what actually happened: which effective Odoo
    partner the review now uses and, for ``create_permanent_supplier``, whether
    that partner was newly created or already existed. History stays truthful --
    the mode is never rewritten to ``match_existing`` after a create.

    Append-only: no UPDATE path, no DELETE in the normal workflow.
    """

    __tablename__ = "workbench_review_supplier_remediation_effects"
    __table_args__ = (
        ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_workbench_review_supplier_remediation_effects_review_id",
        ),
        CheckConstraint(
            "company_id > 0",
            name="ck_workbench_review_supplier_remediation_effects_company_id_positive",
        ),
        CheckConstraint(
            "review_version > 0",
            name="ck_workbench_review_supplier_remediation_effects_review_version_positive",
        ),
        CheckConstraint(
            "resolved_partner_id > 0",
            name="ck_workbench_review_supplier_remediation_effects_partner_id_positive",
        ),
        CheckConstraint(
            "partner_write_status IN ('created', 'already_exists', 'selected')",
            name="ck_workbench_review_supplier_remediation_effects_write_status",
        ),
        CheckConstraint(
            "(mode = 'create_permanent_supplier' AND partner_write_status IN ('created', 'already_exists')) "
            "OR (mode = 'match_existing' AND partner_write_status = 'selected')",
            name="ck_workbench_review_supplier_remediation_effects_status_by_mode",
        ),
        UniqueConstraint(
            "review_id",
            "review_version",
            name="uq_workbench_review_supplier_remediation_effects_review_version",
        ),
        Index(
            "ix_workbench_review_supplier_remediation_effects_company_review",
            "company_id",
            "review_id",
        ),
        Index(
            "ix_workbench_review_supplier_remediation_effects_partner_id",
            "resolved_partner_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    review_version: Mapped[int] = mapped_column(nullable=False)
    source_invoice_id: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    resolved_partner_id: Mapped[int] = mapped_column(nullable=False)
    partner_write_status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_supplier_tax_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
