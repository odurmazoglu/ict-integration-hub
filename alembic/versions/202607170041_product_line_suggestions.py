"""PR F / F2: advisory product suggestion sets and candidates (ADR-0014)

Revision ID: 202607170041
Revises: 202607170040
Create Date: 2026-10-13 12:00:00.000000

* ``workbench_product_line_suggestion_sets``: one advisory generation per Workbench product
  line version, with its input digest, catalog and supplierinfo fingerprints, engine
  version, status (``current`` / ``superseded`` / ``invalidated``) and validation time. A
  partial unique index allows exactly one ``current`` set per (company, review, version, line).
* ``workbench_product_line_suggestion_candidates``: at most 3 ranked candidates per set.

Additive only: no existing table, column, index or constraint is changed, and no existing
code path reads these tables. Advisory rows are not audit history of any business write,
so the downgrade simply drops both tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170041"
down_revision: str | None = "202607170040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SETS = "workbench_product_line_suggestion_sets"
_CANDIDATES = "workbench_product_line_suggestion_candidates"
_STATUSES = ("current", "superseded", "invalidated")
_REASONS = (
    "review_version_changed",
    "line_resolved",
    "review_not_pending",
    "input_changed",
    "catalog_changed",
    "supplierinfo_changed",
    "engine_changed",
)
_TIERS = ("t0_stale_supplierinfo", "t1_exact_identifier", "t2_prior_mapping", "t3_name_similarity")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        _SETS,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.String(length=255), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("line_number", sa.String(length=255), nullable=False),
        sa.Column("line_key", sa.String(length=512), nullable=False),
        sa.Column("supplier_partner_id", sa.Integer(), nullable=True),
        sa.Column("seller_item_code", sa.String(length=255), nullable=True),
        sa.Column("input_digest", sa.String(length=71), nullable=False),
        sa.Column("catalog_fingerprint", sa.String(length=71), nullable=False),
        sa.Column("supplierinfo_fingerprint", sa.String(length=71), nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("ambiguous", sa.Boolean(), nullable=False),
        sa.Column("reclassification_recommended", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("status_reason", sa.String(length=32), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["review_id"], ["workbench_review_items.review_id"], name="fk_wpls_review_id"),
        sa.CheckConstraint("company_id > 0", name="ck_wpls_company_positive"),
        sa.CheckConstraint("review_version > 0", name="ck_wpls_version_positive"),
        sa.CheckConstraint("supplier_partner_id IS NULL OR supplier_partner_id > 0", name="ck_wpls_partner_positive"),
        sa.CheckConstraint(_in("status", _STATUSES), name="ck_wpls_status"),
        sa.CheckConstraint(
            "(status = 'current' AND status_reason IS NULL) OR "
            f"(status <> 'current' AND {_in('status_reason', _REASONS)})",
            name="ck_wpls_status_reason",
        ),
    )
    op.create_index(
        "ux_wpls_current_line",
        _SETS,
        ["company_id", "review_id", "review_version", "line_number"],
        unique=True,
        sqlite_where=sa.text("status = 'current'"),
        postgresql_where=sa.text("status = 'current'"),
    )
    op.create_index("ix_wpls_company_status", _SETS, ["company_id", "status"])
    op.create_index("ix_wpls_status_changed", _SETS, ["status", "status_changed_at"])

    op.create_table(
        _CANDIDATES,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("set_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("tier", sa.String(length=32), nullable=False),
        sa.Column("score", sa.Numeric(precision=3, scale=2), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["set_id"], [f"{_SETS}.id"], name="fk_wplsc_set_id", ondelete="CASCADE"),
        sa.UniqueConstraint("set_id", "product_id", name="uq_wplsc_set_product"),
        sa.CheckConstraint("rank BETWEEN 1 AND 3", name="ck_wplsc_rank"),
        sa.CheckConstraint("product_id > 0", name="ck_wplsc_product_positive"),
        sa.CheckConstraint("score >= 0 AND score <= 1", name="ck_wplsc_score"),
        sa.CheckConstraint(_in("tier", _TIERS), name="ck_wplsc_tier"),
    )
    op.create_index("ix_workbench_product_line_suggestion_candidates_set_id", _CANDIDATES, ["set_id"])


def downgrade() -> None:
    op.drop_index("ix_workbench_product_line_suggestion_candidates_set_id", table_name=_CANDIDATES)
    op.drop_table(_CANDIDATES)
    op.drop_index("ix_wpls_status_changed", table_name=_SETS)
    op.drop_index("ix_wpls_company_status", table_name=_SETS)
    op.drop_index("ux_wpls_current_line", table_name=_SETS)
    op.drop_table(_SETS)
