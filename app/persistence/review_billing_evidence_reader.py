from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.workbench.exceptions import (
    ReviewDataIntegrityError,
    ReviewNotFoundError,
    ReviewPersistenceError,
    WorkbenchContractError,
)
from app.billing.dto import CustomerInvoiceBillingInstruction, CustomerInvoiceBillingLine
from app.models.workbench_review_billing_evidence import WorkbenchReviewBillingEvidence

SAFE_BILLING_ERROR = "Review billing evidence could not be loaded safely."
SAFE_BILLING_NOT_FOUND = "Review billing evidence was not found."
SAFE_BILLING_INTEGRITY_ERROR = "Review billing evidence is invalid."
REVIEW_BILLING_EVIDENCE_SCHEMA_VERSION = 1


class SqlAlchemyReviewBillingEvidenceReader:
    """Load version-pinned customer billing evidence from Hub persistence."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_billing_instructions(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> tuple[CustomerInvoiceBillingInstruction, ...]:
        _validate_query(review_id=review_id, company_id=company_id, review_version=review_version)
        try:
            records = tuple(
                self._session.scalars(
                    select(WorkbenchReviewBillingEvidence)
                    .where(
                        WorkbenchReviewBillingEvidence.review_id == review_id,
                        WorkbenchReviewBillingEvidence.company_id == company_id,
                        WorkbenchReviewBillingEvidence.review_version == review_version,
                    )
                    .order_by(WorkbenchReviewBillingEvidence.billing_key.asc(), WorkbenchReviewBillingEvidence.id.asc())
                )
            )
            if not records:
                raise ReviewNotFoundError(SAFE_BILLING_NOT_FOUND)
            instructions = tuple(_billing_instruction_from_record(record) for record in records)
            _validate_unique_billing_keys(instructions)
            return instructions
        except (ReviewNotFoundError, ReviewDataIntegrityError):
            raise
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_BILLING_ERROR) from exc
        except (InvalidOperation, KeyError, TypeError, ValueError, WorkbenchContractError) as exc:
            raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR) from exc


def serialize_billing_instruction_payload(instruction: CustomerInvoiceBillingInstruction) -> dict[str, Any]:
    if not isinstance(instruction, CustomerInvoiceBillingInstruction):
        raise WorkbenchContractError("CustomerInvoiceBillingInstruction DTO is required.")
    return {
        "billing_key": instruction.billing_key,
        "customer_id": instruction.customer_id,
        "currency": instruction.currency,
        "lines": [
            {
                "allocation_key": line.allocation_key,
                "product_id": line.product_id,
                "description": line.description,
                "quantity": _decimal_to_data(line.quantity),
                "unit_price": _decimal_to_data(line.unit_price),
                "sales_tax_ids": list(line.sales_tax_ids),
            }
            for line in instruction.lines
        ],
    }


def deserialize_billing_instruction_payload(data: dict[str, Any]) -> CustomerInvoiceBillingInstruction:
    return CustomerInvoiceBillingInstruction(
        billing_key=_required_text(data.get("billing_key")),
        customer_id=_required_int(data.get("customer_id")),
        currency=_required_text(data.get("currency")),
        lines=tuple(_billing_line_from_data(_require_dict(line)) for line in _list(data.get("lines"))),
    )


def _billing_instruction_from_record(record: WorkbenchReviewBillingEvidence) -> CustomerInvoiceBillingInstruction:
    if record.schema_version != REVIEW_BILLING_EVIDENCE_SCHEMA_VERSION:
        raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR)
    instruction = deserialize_billing_instruction_payload(_require_dict(record.billing_instruction))
    if instruction.billing_key != record.billing_key:
        raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR)
    return instruction


def _billing_line_from_data(data: dict[str, Any]) -> CustomerInvoiceBillingLine:
    return CustomerInvoiceBillingLine(
        allocation_key=_required_text(data.get("allocation_key")),
        product_id=_required_int(data.get("product_id")),
        description=_required_text(data.get("description")),
        quantity=_required_decimal(data.get("quantity")),
        unit_price=_required_decimal(data.get("unit_price")),
        sales_tax_ids=tuple(_required_int(value) for value in _list(data.get("sales_tax_ids"))),
    )


def _validate_query(*, review_id: str, company_id: int, review_version: int) -> None:
    _required_text(review_id)
    if type(company_id) is not int or company_id <= 0:
        raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR)
    if type(review_version) is not int or review_version <= 0:
        raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR)


def _validate_unique_billing_keys(instructions: tuple[CustomerInvoiceBillingInstruction, ...]) -> None:
    billing_keys = tuple(instruction.billing_key for instruction in instructions)
    if len(set(billing_keys)) != len(billing_keys):
        raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR)


def _decimal_to_data(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        raise WorkbenchContractError("Decimal value is required.")
    return str(value)


def _required_decimal(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR)
    return Decimal(value)


def _required_int(value: Any) -> int:
    if type(value) is not int:
        raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR)
    return value


def _required_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR)
    return value


def _require_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR)
    return value


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ReviewDataIntegrityError(SAFE_BILLING_INTEGRITY_ERROR)
    return value
