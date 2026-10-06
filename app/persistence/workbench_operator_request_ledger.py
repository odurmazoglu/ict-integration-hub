"""SQLAlchemy ledger for Workbench operator requests (ADR-0013).

Uses its own session (never the use cases' business session), so ledger commits and
rollbacks are independent of any use case transaction.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workbench.operator_request_ingestion import (
    OperatorRequest,
    OperatorRequestLedgerEntry,
    OperatorRequestLedgerStatus,
)
from app.models.workbench_operator_request import WorkbenchOperatorRequest

SAFE_LEDGER_ERROR = "Workbench operator request ledger could not be updated."


class OperatorRequestLedgerError(WorkbenchContractError):
    safe_message = SAFE_LEDGER_ERROR


class SqlAlchemyOperatorRequestLedger:
    def __init__(self, session: Session) -> None:
        self._session = session

    def find(self, request_key: str) -> OperatorRequestLedgerEntry | None:
        record = self._get(request_key)
        return _entry(record) if record is not None else None

    def start(self, *, request_key: str, request: OperatorRequest, actor: str | None) -> OperatorRequestLedgerEntry:
        record = WorkbenchOperatorRequest(
            request_key=request_key,
            company_id=request.company_id,
            review_id=request.review_id,
            odoo_record_id=request.odoo_record_id,
            action=request.action.value,
            expected_version=request.expected_version,
            requested_by_odoo_user_id=request.requested_by_odoo_user_id,
            requested_at=request.requested_at,
            actor=actor,
            status=OperatorRequestLedgerStatus.IN_PROGRESS.value,
            attempts=0,
        )
        try:
            self._session.add(record)
            self._session.flush()
        except IntegrityError:
            # A concurrent tick inserted the same request first; continue with that row.
            self._session.rollback()
            existing = self._get(request_key)
            if existing is None:
                raise OperatorRequestLedgerError(SAFE_LEDGER_ERROR) from None
            return _entry(existing)
        except SQLAlchemyError as exc:
            raise OperatorRequestLedgerError(SAFE_LEDGER_ERROR) from exc
        return _entry(record)

    def record_attempt(self, request_key: str, *, message: str) -> OperatorRequestLedgerEntry:
        record = self._require(request_key)
        record.attempts += 1
        record.message = message
        self._flush()
        return _entry(record)

    def record_authorization(self, request_key: str, *, authorization_id: str) -> None:
        record = self._require(request_key)
        if record.authorization_id is not None and record.authorization_id != authorization_id:
            raise OperatorRequestLedgerError("Operator request already has a different write authorization.")
        record.authorization_id = authorization_id
        self._flush()

    def finish(self, request_key: str, *, status: OperatorRequestLedgerStatus, message: str) -> None:
        if status is OperatorRequestLedgerStatus.IN_PROGRESS:
            raise OperatorRequestLedgerError("finish requires a terminal status.")
        record = self._require(request_key)
        record.status = status.value
        record.message = message
        self._flush()

    def commit(self) -> None:
        try:
            self._session.commit()
        except SQLAlchemyError as exc:
            self._session.rollback()
            raise OperatorRequestLedgerError(SAFE_LEDGER_ERROR) from exc

    def rollback(self) -> None:
        self._session.rollback()

    def _get(self, request_key: str) -> WorkbenchOperatorRequest | None:
        try:
            return self._session.scalar(
                select(WorkbenchOperatorRequest).where(WorkbenchOperatorRequest.request_key == request_key)
            )
        except SQLAlchemyError as exc:
            raise OperatorRequestLedgerError(SAFE_LEDGER_ERROR) from exc

    def _require(self, request_key: str) -> WorkbenchOperatorRequest:
        record = self._get(request_key)
        if record is None:
            raise OperatorRequestLedgerError("Operator request ledger row is missing.")
        return record

    def _flush(self) -> None:
        try:
            self._session.flush()
        except SQLAlchemyError as exc:
            self._session.rollback()
            raise OperatorRequestLedgerError(SAFE_LEDGER_ERROR) from exc


def _entry(record: WorkbenchOperatorRequest) -> OperatorRequestLedgerEntry:
    return OperatorRequestLedgerEntry(
        request_key=record.request_key,
        status=OperatorRequestLedgerStatus(record.status),
        attempts=record.attempts,
        message=record.message,
        authorization_id=record.authorization_id,
    )
