"""Deterministic Hub-owned operating-expense mapping foundation.

This package holds only the persistent mapping data contract and its read port.
It carries no classification or execution behavior: nothing here is wired into
the rule engine, decision engine, builder, or execution runtime yet.
"""

from app.application.expense_mapping.contracts import OperatingExpenseMapping
from app.application.expense_mapping.exceptions import (
    OperatingExpenseMappingContractError,
    OperatingExpenseMappingDataIntegrityError,
    OperatingExpenseMappingError,
)
from app.application.expense_mapping.repository import OperatingExpenseMappingRepository

__all__ = [
    "OperatingExpenseMapping",
    "OperatingExpenseMappingContractError",
    "OperatingExpenseMappingDataIntegrityError",
    "OperatingExpenseMappingError",
    "OperatingExpenseMappingRepository",
]
