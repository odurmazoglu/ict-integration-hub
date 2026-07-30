from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.application.dto.base import ApplicationDTO


class WorkflowType(StrEnum):
    """Canonical procurement workflow identifiers used across ICT IPP."""

    VENDOR_BILL = "vendor_bill"
    RFQ = "rfq"
    EXPENSE = "expense"
    ASSET = "asset"
    SUBSCRIPTION = "subscription"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True, slots=True)
class WorkflowDecision(ApplicationDTO):
    """Deterministic workflow selection output consumed by orchestration."""

    workflow: WorkflowType
    matched_rule: str | None = None
    explanation: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
