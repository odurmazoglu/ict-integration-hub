"""fixed-asset (CAPITALIZE_FIXED_ASSET) accounting resolution + frozen evidence

Revision ID: 202607170035
Revises: 202607170034
Create Date: 2026-10-05 12:00:00.000000

``workbench_review_accounting_resolutions``: ``asset_account_id`` and
``depreciation_model_id`` (nullable), ``expense_account_id``/``expense_category``
become nullable, the treatment check also allows ``capitalize_fixed_asset`` and a new
shape check enforces exactly one field set per treatment. Every existing row is an
``expense_account`` row with both expense fields set and both new columns NULL, so it
satisfies the shape check unchanged -- no historical row is rewritten.

``workbench_review_execution_evidence`` / ``execution_source_invoice_evidence``: one
nullable JSON ``fixed_asset_accounting`` column each (NULL for every existing row).

Downgrade refuses to run while any ``capitalize_fixed_asset`` resolution exists (it
could not be represented by the old schema) instead of deleting accounting evidence.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607170035"
down_revision: str | None = "202607170034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESOLUTIONS = "workbench_review_accounting_resolutions"
_EVIDENCE_TABLES = ("workbench_review_execution_evidence", "execution_source_invoice_evidence")
_TREATMENT_CK = "ck_workbench_review_accounting_resolutions_treatment_type"
_SHAPE_CK = "ck_workbench_review_accounting_resolutions_treatment_shape"
_ASSET_POSITIVE_CK = "ck_workbench_review_accounting_resolutions_asset_account_id_positive"
_MODEL_POSITIVE_CK = "ck_workbench_review_accounting_resolutions_depreciation_model_id_positive"
_SHAPE_SQL = (
    "(treatment_type = 'expense_account' AND expense_account_id IS NOT NULL "
    "AND expense_category IS NOT NULL AND asset_account_id IS NULL AND depreciation_model_id IS NULL) "
    "OR (treatment_type = 'capitalize_fixed_asset' AND asset_account_id IS NOT NULL "
    "AND depreciation_model_id IS NOT NULL AND expense_account_id IS NULL AND expense_category IS NULL)"
)


def upgrade() -> None:
    with op.batch_alter_table(_RESOLUTIONS) as batch_op:
        batch_op.add_column(sa.Column("asset_account_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("depreciation_model_id", sa.Integer(), nullable=True))
        batch_op.alter_column("expense_account_id", existing_type=sa.Integer(), nullable=True)
        batch_op.alter_column("expense_category", existing_type=sa.String(length=64), nullable=True)
        batch_op.drop_constraint(_TREATMENT_CK, type_="check")
        batch_op.create_check_constraint(
            _TREATMENT_CK, "treatment_type IN ('expense_account', 'capitalize_fixed_asset')"
        )
        batch_op.create_check_constraint(_SHAPE_CK, _SHAPE_SQL)
        batch_op.create_check_constraint(_ASSET_POSITIVE_CK, "asset_account_id IS NULL OR asset_account_id > 0")
        batch_op.create_check_constraint(
            _MODEL_POSITIVE_CK, "depreciation_model_id IS NULL OR depreciation_model_id > 0"
        )
    for table in _EVIDENCE_TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(sa.Column("fixed_asset_accounting", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    fixed_asset_rows = bind.execute(
        sa.text(f"SELECT count(*) FROM {_RESOLUTIONS} WHERE treatment_type = 'capitalize_fixed_asset'")
    ).scalar()
    if fixed_asset_rows:
        raise RuntimeError(
            "Refusing to downgrade: capitalize_fixed_asset accounting resolutions exist and cannot be "
            "represented by the previous schema."
        )
    for table in _EVIDENCE_TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("fixed_asset_accounting")
    with op.batch_alter_table(_RESOLUTIONS) as batch_op:
        batch_op.drop_constraint(_MODEL_POSITIVE_CK, type_="check")
        batch_op.drop_constraint(_ASSET_POSITIVE_CK, type_="check")
        batch_op.drop_constraint(_SHAPE_CK, type_="check")
        batch_op.drop_constraint(_TREATMENT_CK, type_="check")
        batch_op.create_check_constraint(_TREATMENT_CK, "treatment_type IN ('expense_account')")
        batch_op.alter_column("expense_category", existing_type=sa.String(length=64), nullable=False)
        batch_op.alter_column("expense_account_id", existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column("depreciation_model_id")
        batch_op.drop_column("asset_account_id")
