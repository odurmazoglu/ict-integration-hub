from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class QuotationScenarioEvidence(Base):
    """Immutable Hub-owned evidence for one captured customer quotation scenario.

    Semantic identity is ``(company_id, review_id, decision_id, decision_version,
    scenario_id)`` and is enforced by a database unique constraint. The canonical
    :class:`app.application.quotation.contracts.QuotationScenarioSnapshot` is stored
    verbatim in ``scenario_snapshot`` (monetary and quantity values serialized as
    canonical decimal strings, line order preserved) so it reconstructs exactly.
    There is no foreign key to mutable Odoo authoring records.
    """

    __tablename__ = "quotation_scenario_evidence"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "review_id",
            "decision_id",
            "decision_version",
            "scenario_id",
            name="uq_quotation_scenario_evidence_semantic_identity",
        ),
        CheckConstraint("company_id > 0", name="ck_quotation_scenario_evidence_company_positive"),
        CheckConstraint(
            "decision_version > 0",
            name="ck_quotation_scenario_evidence_decision_version_positive",
        ),
        CheckConstraint(
            "schema_version > 0",
            name="ck_quotation_scenario_evidence_schema_version_positive",
        ),
        Index(
            "ix_quotation_scenario_evidence_company_review_decision",
            "company_id",
            "review_id",
            "decision_id",
            "decision_version",
        ),
        Index("ix_quotation_scenario_evidence_scenario_id", "scenario_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(255), nullable=False)
    decision_version: Mapped[int] = mapped_column(nullable=False)
    scenario_id: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[int] = mapped_column(nullable=False, default=1)
    scenario_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
