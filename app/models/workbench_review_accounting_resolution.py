from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class WorkbenchReviewAccountingResolution(Base):
    """Immutable, review-scoped record of *how a purchase should be posted* (P0-PROD-15T).

    The review-scoped escape hatch for a mixed-purpose supplier: unlike
    ``operating_expense_mappings`` (keyed by ``(company_id, vendor_partner_id)``,
    supplier-wide), this row applies to exactly one review version and never
    contaminates a future invoice from the same supplier.

    ``treatment_type`` is ``'expense_account'`` (expense_account_id + expense_category)
    or ``'capitalize_fixed_asset'`` (asset_account_id + depreciation_model_id); the
    shape check constraint enforces exactly one field set per treatment, so historical
    expense rows stay valid unchanged.

    Append-only: no UPDATE path, no DELETE in the normal workflow.
    """

    __tablename__ = "workbench_review_accounting_resolutions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["review_id"],
            ["workbench_review_items.review_id"],
            name="fk_workbench_review_accounting_resolutions_review_id",
        ),
        CheckConstraint(
            "company_id > 0",
            name="ck_workbench_review_accounting_resolutions_company_id_positive",
        ),
        CheckConstraint(
            "review_version > 0",
            name="ck_workbench_review_accounting_resolutions_review_version_positive",
        ),
        CheckConstraint(
            "treatment_type IN ('expense_account', 'capitalize_fixed_asset')",
            name="ck_workbench_review_accounting_resolutions_treatment_type",
        ),
        CheckConstraint(
            "(treatment_type = 'expense_account' AND expense_account_id IS NOT NULL "
            "AND expense_category IS NOT NULL AND asset_account_id IS NULL AND depreciation_model_id IS NULL) "
            "OR (treatment_type = 'capitalize_fixed_asset' AND asset_account_id IS NOT NULL "
            "AND depreciation_model_id IS NOT NULL AND expense_account_id IS NULL AND expense_category IS NULL)",
            name="ck_workbench_review_accounting_resolutions_treatment_shape",
        ),
        CheckConstraint(
            "asset_account_id IS NULL OR asset_account_id > 0",
            name="ck_workbench_review_accounting_resolutions_asset_account_id_positive",
        ),
        CheckConstraint(
            "depreciation_model_id IS NULL OR depreciation_model_id > 0",
            name="ck_workbench_review_accounting_resolutions_depreciation_model_id_positive",
        ),
        CheckConstraint(
            "expense_account_id > 0",
            name="ck_workbench_review_accounting_resolutions_expense_account_id_positive",
        ),
        CheckConstraint(
            "length(trim(expense_category)) > 0",
            name="ck_workbench_review_accounting_resolutions_expense_category_not_empty",
        ),
        UniqueConstraint(
            "review_id",
            "review_version",
            name="uq_workbench_review_accounting_resolutions_review_version",
        ),
        Index(
            "ix_workbench_review_accounting_resolutions_company_review",
            "company_id",
            "review_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    review_version: Mapped[int] = mapped_column(nullable=False)
    treatment_type: Mapped[str] = mapped_column(String(32), nullable=False)
    expense_account_id: Mapped[int | None] = mapped_column(nullable=True)
    expense_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    asset_account_id: Mapped[int | None] = mapped_column(nullable=True)
    depreciation_model_id: Mapped[int | None] = mapped_column(nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    note: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
