from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.application.expense_mapping.contracts import EXPENSE_CATEGORY_PATTERN, OperatingExpenseMapping
from app.application.expense_mapping.exceptions import OperatingExpenseMappingError


class OperatingExpenseMappingConflictError(OperatingExpenseMappingError):
    """A different enabled mapping already exists for the supplier."""

    error_category = "operating_expense_mapping_conflict_error"


class OperatingExpenseMappingOnboardingOutcome(StrEnum):
    CREATED = "created"
    ALREADY_CONFIGURED = "already_configured"


@dataclass(frozen=True, slots=True)
class OnboardOperatingExpenseMappingCommand:
    """Explicit accounting master data supplied by an operator; never inferred."""

    company_id: int
    vendor_partner_id: int
    expense_account_id: int
    expense_category: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class OperatingExpenseMappingOnboardingResult:
    outcome: OperatingExpenseMappingOnboardingOutcome
    mapping: OperatingExpenseMapping


class OperatingExpenseMappingOnboardingRepository(Protocol):
    def find_for_supplier(self, *, company_id: int, vendor_partner_id: int) -> OperatingExpenseMapping | None:
        pass

    def create(
        self,
        *,
        company_id: int,
        vendor_partner_id: int,
        expense_account_id: int,
        expense_category: str,
        enabled: bool,
    ) -> OperatingExpenseMapping:
        pass


class OnboardOperatingExpenseMappingUseCase:
    """Controlled, idempotent onboarding of one supplier-level operating-expense mapping.

    The expense account is never inferred. An identical request is a no-op; a request that
    diverges from an existing enabled mapping's ``expense_account_id`` / ``expense_category``
    fails closed. Changing an existing enabled mapping is a separate administrative action,
    not part of this path.
    """

    def __init__(self, repository: OperatingExpenseMappingOnboardingRepository) -> None:
        self._repository = repository

    def execute(self, command: OnboardOperatingExpenseMappingCommand) -> OperatingExpenseMappingOnboardingResult:
        _validate(command)
        category = command.expense_category.strip()
        existing = self._repository.find_for_supplier(
            company_id=command.company_id,
            vendor_partner_id=command.vendor_partner_id,
        )
        if existing is not None:
            if (
                existing.expense_account_id == command.expense_account_id
                and existing.expense_category == category
                and existing.enabled == command.enabled
            ):
                return OperatingExpenseMappingOnboardingResult(
                    OperatingExpenseMappingOnboardingOutcome.ALREADY_CONFIGURED, existing
                )
            raise OperatingExpenseMappingConflictError(
                "An enabled operating-expense mapping with different accounting values already "
                "exists for this supplier; change it through a separate administrative action."
            )
        created = self._repository.create(
            company_id=command.company_id,
            vendor_partner_id=command.vendor_partner_id,
            expense_account_id=command.expense_account_id,
            expense_category=category,
            enabled=command.enabled,
        )
        return OperatingExpenseMappingOnboardingResult(OperatingExpenseMappingOnboardingOutcome.CREATED, created)


def _validate(command: OnboardOperatingExpenseMappingCommand) -> None:
    for name, value in (
        ("company_id", command.company_id),
        ("vendor_partner_id", command.vendor_partner_id),
        ("expense_account_id", command.expense_account_id),
    ):
        if type(value) is not int or value <= 0:
            raise OperatingExpenseMappingError(f"{name} must be a positive integer.")
    if type(command.enabled) is not bool:
        raise OperatingExpenseMappingError("enabled must be a boolean.")
    if not isinstance(command.expense_category, str) or not EXPENSE_CATEGORY_PATTERN.match(
        command.expense_category.strip()
    ):
        raise OperatingExpenseMappingError(
            "expense_category must be a short uppercase code matching ^[A-Z][A-Z0-9_]{0,63}$."
        )
