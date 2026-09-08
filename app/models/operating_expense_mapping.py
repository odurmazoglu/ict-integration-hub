from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Index, String, func, text, true
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class OperatingExpenseMappingRecord(Base):
    """Deterministic Hub-owned mapping: (company_id, vendor_partner_id) -> expense account.

    At most one enabled row may exist per ``(company_id, vendor_partner_id)``,
    enforced by a partial unique index so disabled historical rows may coexist.
    Integer ids are external Odoo identifiers; there are no ERP foreign keys.
    """

    __tablename__ = "operating_expense_mappings"
    __table_args__ = (
        CheckConstraint("company_id > 0", name="ck_operating_expense_mappings_company_id_positive"),
        CheckConstraint(
            "vendor_partner_id > 0",
            name="ck_operating_expense_mappings_vendor_partner_id_positive",
        ),
        CheckConstraint(
            "expense_account_id > 0",
            name="ck_operating_expense_mappings_expense_account_id_positive",
        ),
        CheckConstraint(
            "length(trim(expense_category)) > 0",
            name="ck_operating_expense_mappings_expense_category_not_empty",
        ),
        Index(
            "ix_operating_expense_mappings_company_partner",
            "company_id",
            "vendor_partner_id",
        ),
        Index(
            "uq_operating_expense_mappings_active_supplier",
            "company_id",
            "vendor_partner_id",
            unique=True,
            sqlite_where=text("enabled"),
            postgresql_where=text("enabled"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    vendor_partner_id: Mapped[int] = mapped_column(nullable=False)
    expense_account_id: Mapped[int] = mapped_column(nullable=False)
    expense_category: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, server_default=true(), default=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        AwareDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
