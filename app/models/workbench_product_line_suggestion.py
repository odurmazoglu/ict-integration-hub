from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime

#: ``current`` is the one displayable set of a review line version; the others are history.
SUGGESTION_SET_STATUSES = ("current", "superseded", "invalidated")
#: Why a set stopped being current (NULL while current).
SUGGESTION_SET_REASONS = (
    "review_version_changed",
    "line_resolved",
    "review_not_pending",
    "input_changed",
    "catalog_changed",
    "supplierinfo_changed",
    "engine_changed",
)
#: Must equal ``app.advisory.product_suggestions.SuggestionTier`` values (guarded by a test).
SUGGESTION_TIERS = ("t0_stale_supplierinfo", "t1_exact_identifier", "t2_prior_mapping", "t3_name_similarity")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


class WorkbenchProductLineSuggestionSet(Base):
    """PR F / F2: one advisory suggestion generation for one Workbench product line (ADR-0014).

    Advisory only: never read by matching, decisions, execution, product mapping or
    creation. At most one ``current`` row per ``(company, review, version, line)``
    (partial unique index); every older generation is kept as ``superseded`` or
    ``invalidated`` history until the dry-run-first cleanup removes it after 90 days.
    ``validated_at`` is the last time a successful Odoo catalog read confirmed the set's
    fingerprints; display validity is bounded from it, never from ``generated_at``.
    """

    __tablename__ = "workbench_product_line_suggestion_sets"
    __table_args__ = (
        ForeignKeyConstraint(["review_id"], ["workbench_review_items.review_id"], name="fk_wpls_review_id"),
        CheckConstraint("company_id > 0", name="ck_wpls_company_positive"),
        CheckConstraint("review_version > 0", name="ck_wpls_version_positive"),
        CheckConstraint("supplier_partner_id IS NULL OR supplier_partner_id > 0", name="ck_wpls_partner_positive"),
        CheckConstraint(_in("status", SUGGESTION_SET_STATUSES), name="ck_wpls_status"),
        CheckConstraint(
            "(status = 'current' AND status_reason IS NULL) OR "
            f"(status <> 'current' AND {_in('status_reason', SUGGESTION_SET_REASONS)})",
            name="ck_wpls_status_reason",
        ),
        Index(
            "ux_wpls_current_line",
            "company_id",
            "review_id",
            "review_version",
            "line_number",
            unique=True,
            sqlite_where=text("status = 'current'"),
            postgresql_where=text("status = 'current'"),
        ),
        Index("ix_wpls_company_status", "company_id", "status"),
        Index("ix_wpls_status_changed", "status", "status_changed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    review_version: Mapped[int] = mapped_column(nullable=False)
    line_number: Mapped[str] = mapped_column(String(255), nullable=False)
    line_key: Mapped[str] = mapped_column(String(512), nullable=False)
    supplier_partner_id: Mapped[int | None] = mapped_column(nullable=True)
    seller_item_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: SHA-256 of the line inputs (supplier, seller code, manufacturer code, description).
    input_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    catalog_fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    supplierinfo_fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    ambiguous: Mapped[bool] = mapped_column(nullable=False, default=False)
    reclassification_recommended: Mapped[bool] = mapped_column(nullable=False, default=False)
    notes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    status_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    validated_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    status_changed_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), nullable=True)


class WorkbenchProductLineSuggestionCandidate(Base):
    """PR F / F2: one ranked advisory candidate (at most 3 per set)."""

    __tablename__ = "workbench_product_line_suggestion_candidates"
    __table_args__ = (
        UniqueConstraint("set_id", "product_id", name="uq_wplsc_set_product"),
        CheckConstraint("rank BETWEEN 1 AND 3", name="ck_wplsc_rank"),
        CheckConstraint("product_id > 0", name="ck_wplsc_product_positive"),
        CheckConstraint("score >= 0 AND score <= 1", name="ck_wplsc_score"),
        CheckConstraint(_in("tier", SUGGESTION_TIERS), name="ck_wplsc_tier"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    set_id: Mapped[int] = mapped_column(
        ForeignKey("workbench_product_line_suggestion_sets.id", ondelete="CASCADE", name="fk_wplsc_set_id"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(nullable=False)
    rank: Mapped[int] = mapped_column(nullable=False)
    product_id: Mapped[int] = mapped_column(nullable=False)
    tier: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    reasons: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    warnings: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
