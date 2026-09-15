from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class WorkbenchReviewProductIdentityClaim(Base):
    """Cross-review lock for one supplier-product identity (P0-PROD-07G).

    One row per ``(company_id, resolved_supplier_partner_id, seller_item_code)`` --
    the DB-enforced barrier preventing two different reviews from racing to create
    two Odoo products for the same supplier item. Purely a uniqueness lock + owner
    pointer: the product/supplierinfo identity itself always lives on the owner's
    ``workbench_review_product_remediation_reservations`` row (single source of
    truth), reached via the composite FK below. Append-only: no UPDATE, no DELETE
    in the normal workflow.
    """

    __tablename__ = "workbench_review_product_identity_claims"
    __table_args__ = (
        # Abbreviated ("wrpr_identity_claims") and kept under PostgreSQL's 63-byte
        # NAMEDATALEN limit -- see workbench_review_product_remediation_reservation.py
        # for why: two similarly-long names on that table silently truncated to an
        # identical prefix on real Postgres and collided.
        ForeignKeyConstraint(
            ["owner_review_id", "owner_company_id", "owner_review_version", "owner_line_number"],
            [
                "workbench_review_product_remediation_reservations.review_id",
                "workbench_review_product_remediation_reservations.company_id",
                "workbench_review_product_remediation_reservations.review_version",
                "workbench_review_product_remediation_reservations.line_number",
            ],
            name="fk_wrpr_identity_claims_owner",
        ),
        CheckConstraint(
            "company_id > 0",
            name="ck_wrpr_identity_claims_company_positive",
        ),
        CheckConstraint(
            "resolved_supplier_partner_id > 0",
            name="ck_wrpr_identity_claims_partner_positive",
        ),
        CheckConstraint(
            "owner_company_id > 0",
            name="ck_wrpr_identity_claims_owner_company_positive",
        ),
        CheckConstraint(
            "owner_review_version > 0",
            name="ck_wrpr_identity_claims_owner_version_positive",
        ),
        UniqueConstraint(
            "company_id",
            "resolved_supplier_partner_id",
            "seller_item_code",
            name="uq_wrpr_identity_claims_identity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    resolved_supplier_partner_id: Mapped[int] = mapped_column(nullable=False)
    seller_item_code: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_company_id: Mapped[int] = mapped_column(nullable=False)
    owner_review_version: Mapped[int] = mapped_column(nullable=False)
    owner_line_number: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
