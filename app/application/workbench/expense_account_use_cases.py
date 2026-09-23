"""Application boundary for the read-only expense-account lookup (P0-PROD-15P)."""

from __future__ import annotations

from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workbench.expense_account_lookup import (
    ExpenseAccountCandidate,
    ListExpenseAccountCandidatesQuery,
)
from app.application.workbench.ports import ExpenseAccountCandidateReader


class ListExpenseAccountCandidatesUseCase:
    """Application boundary for one authenticated expense-account lookup."""

    def __init__(self, *, reader: ExpenseAccountCandidateReader) -> None:
        self._reader = reader

    def execute(self, query: ListExpenseAccountCandidatesQuery) -> tuple[ExpenseAccountCandidate, ...]:
        if not isinstance(query, ListExpenseAccountCandidatesQuery):
            raise WorkbenchContractError("A canonical ListExpenseAccountCandidatesQuery is required.")
        return self._reader.find_candidates(company_id=query.company_id, query=query.query)


__all__ = ["ListExpenseAccountCandidatesUseCase"]
