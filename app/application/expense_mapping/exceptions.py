from __future__ import annotations

from app.application.exceptions import ApplicationError


class OperatingExpenseMappingError(ApplicationError):
    """Base error for operating-expense mapping access failures."""

    error_category = "operating_expense_mapping_error"


class OperatingExpenseMappingContractError(OperatingExpenseMappingError):
    """The operating-expense mapping DTO violates its immutable contract."""

    error_category = "operating_expense_mapping_contract_error"


class OperatingExpenseMappingDataIntegrityError(OperatingExpenseMappingError):
    """Persistence exposed more than one enabled mapping for a supplier."""

    error_category = "operating_expense_mapping_data_integrity_error"
