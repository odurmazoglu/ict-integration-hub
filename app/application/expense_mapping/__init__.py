"""Deterministic Hub-owned operating-expense mapping and classification.

This package holds the persistent mapping data contract, its read port, and the
ERP-independent classification layer (typed match result + matcher). It performs
no persistence I/O itself and is not wired into production composition here.
"""

from app.application.expense_mapping.contracts import OperatingExpenseMapping
from app.application.expense_mapping.evidence_rules import (
    EXACT_OPERATING_EXPENSE_CONFIDENCE,
    operating_expense_evidence_errors,
)
from app.application.expense_mapping.exceptions import (
    OperatingExpenseMappingContractError,
    OperatingExpenseMappingDataIntegrityError,
    OperatingExpenseMappingError,
)
from app.application.expense_mapping.matcher import (
    NullOperatingExpenseMatcher,
    OperatingExpenseMatcher,
)
from app.application.expense_mapping.matching import (
    EXACT_MATCH_CONFIDENCE,
    MATCHED_BY_COMPANY_PARTNER,
    OperatingExpenseMatchingEngine,
    OperatingExpenseMatchResult,
    OperatingExpenseMatchStatus,
)
from app.application.expense_mapping.onboarding import (
    OnboardOperatingExpenseMappingCommand,
    OnboardOperatingExpenseMappingUseCase,
    OperatingExpenseMappingConflictError,
    OperatingExpenseMappingOnboardingOutcome,
    OperatingExpenseMappingOnboardingRepository,
    OperatingExpenseMappingOnboardingResult,
)
from app.application.expense_mapping.predicates import invoice_is_product_identifier_free
from app.application.expense_mapping.repository import OperatingExpenseMappingRepository

__all__ = [
    "EXACT_MATCH_CONFIDENCE",
    "EXACT_OPERATING_EXPENSE_CONFIDENCE",
    "MATCHED_BY_COMPANY_PARTNER",
    "NullOperatingExpenseMatcher",
    "OnboardOperatingExpenseMappingCommand",
    "OnboardOperatingExpenseMappingUseCase",
    "OperatingExpenseMapping",
    "OperatingExpenseMappingConflictError",
    "OperatingExpenseMappingContractError",
    "OperatingExpenseMappingDataIntegrityError",
    "OperatingExpenseMappingError",
    "OperatingExpenseMappingOnboardingOutcome",
    "OperatingExpenseMappingOnboardingRepository",
    "OperatingExpenseMappingOnboardingResult",
    "OperatingExpenseMappingRepository",
    "OperatingExpenseMatcher",
    "OperatingExpenseMatchingEngine",
    "OperatingExpenseMatchResult",
    "OperatingExpenseMatchStatus",
    "invoice_is_product_identifier_free",
    "operating_expense_evidence_errors",
]
