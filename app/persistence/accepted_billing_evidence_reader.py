from __future__ import annotations

from decimal import InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.workbench.exceptions import (
    ReviewDataIntegrityError,
    ReviewNotFoundError,
    ReviewPersistenceError,
)
from app.billing.dto import CustomerInvoiceBillingInstruction
from app.models.execution_customer_billing_evidence import ExecutionCustomerBillingEvidence
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.persistence.review_billing_evidence_reader import (
    REVIEW_BILLING_EVIDENCE_SCHEMA_VERSION,
    SAFE_BILLING_ERROR,
    SAFE_BILLING_INTEGRITY_ERROR,
    SAFE_BILLING_NOT_FOUND,
    deserialize_billing_instruction_payload,
)


class SqlAlchemyAcceptedBillingEvidenceReader:
    """Load Stage 2 accepted customer billing evidence for execution planning."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_billing_instructions(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
        decision_id: str | None,
    ) -> tuple[CustomerInvoiceBillingInstruction, ...]:
        _validate_query(
            review_id=review_id,
            company_id=company_id,
            decision_version=decision_version,
            decision_id=decision_id,
        )
        try:
            if decision_id is None:
                decision = self._accepted_decision(
                    review_id=review_id,
                    company_id=company_id,
                    decision_version=decision_version,
                )
                decision_id = decision.decision_id
            records = self._billing_records(
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
                decision_id=decision_id,
            )
            if not records:
                raise ReviewNotFoundError(SAFE_BILLING_NOT_FOUND)
            instructions = tuple(_instruction_from_record(record) for record in records)
            _validate_unique_billing_keys(instructions)
            return instructions
        except (ReviewNotFoundError, ReviewDataIntegrityError):
            raise
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_BILLING_ERROR) from exc
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR) from exc

    def _accepted_decision(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
    ) -> WorkbenchReviewDecision:
        records = tuple(
            self._session.scalars(
                select(WorkbenchReviewDecision)
                .where(
                    WorkbenchReviewDecision.review_id == review_id,
                    WorkbenchReviewDecision.company_id == company_id,
                    WorkbenchReviewDecision.review_version_after == decision_version,
                )
                .order_by(WorkbenchReviewDecision.id.asc())
                .limit(2)
            )
        )
        if len(records) != 1:
            raise ReviewNotFoundError(SAFE_BILLING_NOT_FOUND)
        return records[0]

    def _billing_records(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
        decision_id: str,
    ) -> tuple[ExecutionCustomerBillingEvidence, ...]:
        return tuple(
            self._session.scalars(
                select(ExecutionCustomerBillingEvidence)
                .where(
                    ExecutionCustomerBillingEvidence.review_id == review_id,
                    ExecutionCustomerBillingEvidence.company_id == company_id,
                    ExecutionCustomerBillingEvidence.decision_version == decision_version,
                    ExecutionCustomerBillingEvidence.decision_id == decision_id,
                )
                .order_by(
                    ExecutionCustomerBillingEvidence.billing_key.asc(),
                    ExecutionCustomerBillingEvidence.id.asc(),
                )
            )
        )


def _instruction_from_record(record: ExecutionCustomerBillingEvidence) -> CustomerInvoiceBillingInstruction:
    if record.schema_version != REVIEW_BILLING_EVIDENCE_SCHEMA_VERSION:
        raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR)
    instruction = deserialize_billing_instruction_payload(_require_dict(record.billing_instruction))
    if instruction.billing_key != record.billing_key:
        raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR)
    return instruction


def _validate_query(
    *,
    review_id: str,
    company_id: int,
    decision_version: int,
    decision_id: str | None,
) -> None:
    _required_text(review_id)
    if type(company_id) is not int or company_id <= 0:
        raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR)
    if type(decision_version) is not int or decision_version <= 0:
        raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR)
    if decision_id is not None:
        _required_text(decision_id)


def _validate_unique_billing_keys(instructions: tuple[CustomerInvoiceBillingInstruction, ...]) -> None:
    billing_keys = tuple(instruction.billing_key for instruction in instructions)
    if len(set(billing_keys)) != len(billing_keys):
        raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR)


def _required_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR)
    return value


def _require_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR)
    return value
