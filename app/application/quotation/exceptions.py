from __future__ import annotations

from app.application.exceptions import ApplicationError


class QuotationEvidenceError(ApplicationError):
    """Safe base error for immutable quotation scenario evidence persistence."""

    error_category = "quotation_evidence_error"


class QuotationEvidencePersistenceError(QuotationEvidenceError):
    """Safe error raised when quotation scenario evidence cannot be stored or loaded."""

    error_category = "quotation_evidence_persistence_error"


class QuotationEvidenceNotFoundError(QuotationEvidenceError):
    """Safe error raised when semantic quotation scenario evidence does not exist."""

    error_category = "quotation_evidence_not_found"


class QuotationEvidenceConflictError(QuotationEvidenceError):
    """Safe error raised when a stored scenario differs from a same-identity replay."""

    error_category = "quotation_evidence_conflict"


class QuotationEvidenceDataIntegrityError(QuotationEvidenceError):
    """Safe error raised when persisted quotation scenario evidence cannot hydrate."""

    error_category = "quotation_evidence_data_integrity_error"


class QuotationScenarioOrchestrationError(QuotationEvidenceError):
    """Safe error raised when accepted-decision quotation scenario capture is inconsistent."""

    error_category = "quotation_scenario_orchestration_error"
