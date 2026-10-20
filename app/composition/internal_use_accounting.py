"""Composition of the INTERNAL_USE product-invoice accounting gate and execution check."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.application.execution.internal_use_execution_accounting import InternalUseExecutionAccountingValidator
from app.application.workbench.internal_use_decision_gate import InternalUseDecisionGate
from app.connectors.odoo.client import OdooJson2Client
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.expense_account_candidate_reader import OdooExpenseAccountCandidateReader
from app.persistence import (
    SqlAlchemyExecutionSourceInvoiceReader,
    SqlAlchemyReviewAccountingResolutionRepository,
    SqlAlchemyReviewPurchasePurposeResolutionRepository,
)


def build_internal_use_decision_gate(*, session: Session, odoo_client: OdooJson2Client) -> InternalUseDecisionGate:
    """The decision-acceptance gate: Hub purpose + accounting resolution, and the same
    read-only, company-scoped expense-account eligibility reader the accounting
    resolution itself was validated with."""

    return InternalUseDecisionGate(
        purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        accounting_resolution_reader=SqlAlchemyReviewAccountingResolutionRepository(session),
        expense_account_reader=OdooExpenseAccountCandidateReader(adapter=OdooReadOnlyAdapter(client=odoo_client)),
    )


def build_internal_use_execution_accounting_validator(
    *, session: Session, odoo_client: OdooJson2Client
) -> InternalUseExecutionAccountingValidator:
    """The pre-write execution check: pin and purpose from Hub persistence, the pinned
    account re-read through the read-only eligibility reader. Nothing here can write."""

    return InternalUseExecutionAccountingValidator(
        pin_reader=SqlAlchemyExecutionSourceInvoiceReader(session),
        purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        expense_account_reader=OdooExpenseAccountCandidateReader(adapter=OdooReadOnlyAdapter(client=odoo_client)),
    )


__all__ = ["build_internal_use_decision_gate", "build_internal_use_execution_accounting_validator"]
