from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime

#: Must equal ``ProductLineCreationStatus`` values (guarded by a regression test).
PRODUCT_LINE_CREATION_STATUSES = ("create_attempted", "product_created", "completed", "needs_reconciliation")
#: Must equal ``NewProductKind`` values (guarded by a regression test).
PRODUCT_LINE_CREATION_KINDS = ("service", "goods", "stocked_goods")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


class WorkbenchProductLineCreation(Base):
    """PR D: durable state of one Workbench line's new-product creation.

    One row per ``(review_id, company_id, review_version, line_number)``. Inserted as
    ``create_attempted`` in the same commit as the supplier-product identity claim and
    the consumed authorization, *before* the Odoo create, and advanced strictly forward
    (compare-and-swap). The immutable operator intent (name, kind, category, UoM,
    Internal Reference) and the Hub-derived identity (supplier partner, seller code) are
    recorded so a resume can only ever continue the exact same product.
    """

    __tablename__ = "workbench_product_line_creations"
    __table_args__ = (
        ForeignKeyConstraint(["review_id"], ["workbench_review_items.review_id"], name="fk_wplc_review_id"),
        CheckConstraint("company_id > 0", name="ck_wplc_company_positive"),
        CheckConstraint("review_version > 0", name="ck_wplc_version_positive"),
        CheckConstraint("supplier_partner_id > 0", name="ck_wplc_partner_positive"),
        CheckConstraint("categ_id > 0", name="ck_wplc_categ_positive"),
        CheckConstraint("uom_id > 0", name="ck_wplc_uom_positive"),
        CheckConstraint(_in("status", PRODUCT_LINE_CREATION_STATUSES), name="ck_wplc_status"),
        CheckConstraint(_in("product_kind", PRODUCT_LINE_CREATION_KINDS), name="ck_wplc_product_kind"),
        CheckConstraint(
            "status NOT IN ('product_created', 'completed') "
            "OR (product_template_id IS NOT NULL AND product_id IS NOT NULL)",
            name="ck_wplc_product_by_status",
        ),
        CheckConstraint("status != 'completed' OR supplierinfo_id IS NOT NULL", name="ck_wplc_supplierinfo_by_status"),
        UniqueConstraint("review_id", "company_id", "review_version", "line_number", name="uq_wplc_line"),
        Index("ix_wplc_company_review", "company_id", "review_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[int] = mapped_column(nullable=False)
    review_version: Mapped[int] = mapped_column(nullable=False)
    line_number: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    supplier_partner_id: Mapped[int] = mapped_column(nullable=False)
    seller_item_code: Mapped[str] = mapped_column(String(255), nullable=False)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    product_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    categ_id: Mapped[int] = mapped_column(nullable=False)
    uom_id: Mapped[int] = mapped_column(nullable=False)
    internal_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: sha256 of the intent; the consumed authorization is bound to it.
    intent_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    supplier_product_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    approved_by: Mapped[str] = mapped_column(String(255), nullable=False)
    create_attempted_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    product_template_id: Mapped[int | None] = mapped_column(nullable=True)
    product_id: Mapped[int | None] = mapped_column(nullable=True)
    supplierinfo_id: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        AwareDateTime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )
