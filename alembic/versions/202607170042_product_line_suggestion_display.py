"""PR F / F3.1: advisory suggestion display snapshot and refresh marker (ADR-0014)

Revision ID: 202607170042
Revises: 202607170041
Create Date: 2026-10-13 18:00:00.000000

* ``workbench_product_line_suggestion_candidates.product_name``: the candidate's Odoo
  ``display_name`` as read by the same catalog snapshot that generated the set, so the
  Workbench summary never needs a live Odoo product read. Older rows stay NULL.
* ``workbench_product_line_suggestion_sets.display_synced_at``: start of the last
  successful Odoo Workbench refresh that reflected the set's display state. It lets the
  database-only display sweep skip already-cleared sets and retry failed clearing; it
  never extends ``validated_at``.

Additive, nullable only: no existing column, index, constraint or row is changed. The
downgrade drops both columns (advisory display data only, no business history).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170042"
down_revision: str | None = "202607170041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SETS = "workbench_product_line_suggestion_sets"
_CANDIDATES = "workbench_product_line_suggestion_candidates"


def upgrade() -> None:
    with op.batch_alter_table(_CANDIDATES) as batch:
        batch.add_column(sa.Column("product_name", sa.String(length=512), nullable=True))
    with op.batch_alter_table(_SETS) as batch:
        batch.add_column(sa.Column("display_synced_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(_SETS) as batch:
        batch.drop_column("display_synced_at")
    with op.batch_alter_table(_CANDIDATES) as batch:
        batch.drop_column("product_name")
