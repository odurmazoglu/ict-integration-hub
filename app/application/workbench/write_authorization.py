from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from app.application.dto import ApplicationDTO
from app.application.exceptions import ApplicationError


class WriteAuthorizationOperationType(StrEnum):
    """Permitted operation types for narrow write authorizations."""

    EXECUTE_VENDOR_BILL = "EXECUTE_VENDOR_BILL"


class WriteAuthorizationStatus(StrEnum):
    """Lifecycle statuses for write authorizations."""

    PENDING = "pending"
    CONSUMED = "consumed"
    REVOKED = "revoked"
    EXPIRED = "expired"


class WriteAuthorizationError(ApplicationError):
    """Base exception for narrow write authorization failures."""

    error_category = "write_authorization_failure"


class WriteAuthorizationNotFoundError(WriteAuthorizationError):
    error_category = "write_authorization_not_found"


class WriteAuthorizationScopeMismatchError(WriteAuthorizationError):
    error_category = "write_authorization_scope_mismatch"


class WriteAuthorizationExpiredError(WriteAuthorizationError):
    error_category = "write_authorization_expired"


class WriteAuthorizationAlreadyConsumedError(WriteAuthorizationError):
    error_category = "write_authorization_already_consumed"


class WriteAuthorizationRevokedError(WriteAuthorizationError):
    error_category = "write_authorization_revoked"


@dataclass(frozen=True, slots=True)
class WriteAuthorizationRecord(ApplicationDTO):
    """Auditable representation of an issued/consumed write authorization."""

    authorization_id: str
    company_id: int
    review_id: str
    operation_type: WriteAuthorizationOperationType
    target_version: int
    status: WriteAuthorizationStatus
    authorized_by: str
    created_at: datetime
    expires_at: datetime
    justification: str | None = None
    consumed_at: datetime | None = None
    consumed_by_trace_id: str | None = None
    consumed_by_execution_id: str | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    use_count: int = 0
    last_used_at: datetime | None = None
    last_used_trace_id: str | None = None

    def __post_init__(self) -> None:
        for value in (self.authorization_id, self.review_id, self.authorized_by):
            if not isinstance(value, str) or not value.strip():
                raise WriteAuthorizationScopeMismatchError("Authorization identity and actor are required.")
        if len(self.authorization_id) > 36:
            raise WriteAuthorizationScopeMismatchError("Authorization identity exceeds its storage contract.")
        if (
            type(self.company_id) is not int
            or self.company_id <= 0
            or type(self.target_version) is not int
            or self.target_version <= 0
        ):
            raise WriteAuthorizationScopeMismatchError("Authorization company/version must be positive integers.")
        if not isinstance(self.operation_type, WriteAuthorizationOperationType) or not isinstance(
            self.status, WriteAuthorizationStatus
        ):
            raise WriteAuthorizationScopeMismatchError("Authorization operation/status must be canonical values.")
        for value in (self.created_at, self.expires_at, self.consumed_at, self.revoked_at, self.last_used_at):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise WriteAuthorizationScopeMismatchError("Authorization timestamps must be timezone-aware.")
        if self.expires_at <= self.created_at or self.use_count < 0:
            raise WriteAuthorizationScopeMismatchError("Authorization lifecycle is invalid.")

    def ensure_execution_scope(
        self, *, company_id: int, review_id: str, decision_version: int, execution_id: str
    ) -> None:
        if (self.company_id, self.review_id, self.operation_type, self.target_version) != (
            company_id,
            review_id,
            WriteAuthorizationOperationType.EXECUTE_VENDOR_BILL,
            decision_version,
        ):
            raise WriteAuthorizationScopeMismatchError(
                "Authorization does not match the exact Vendor Bill execution scope."
            )
        if self.status is not WriteAuthorizationStatus.CONSUMED or self.consumed_by_execution_id != execution_id:
            raise WriteAuthorizationAlreadyConsumedError("Authorization must be bound to this execution.")
        if self.is_expired:
            raise WriteAuthorizationExpiredError("Write authorization has expired.")

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at


class WriteAuthorizationRepository(Protocol):
    """Port for durable write authorization state management."""

    def create(
        self,
        *,
        authorization_id: str,
        company_id: int,
        review_id: str,
        operation_type: WriteAuthorizationOperationType,
        target_version: int,
        authorized_by: str,
        expires_at: datetime,
        justification: str | None = None,
    ) -> WriteAuthorizationRecord: ...

    def get_by_id(
        self,
        *,
        authorization_id: str,
        company_id: int,
    ) -> WriteAuthorizationRecord | None: ...

    def list_for_review(
        self,
        *,
        review_id: str,
        company_id: int,
    ) -> tuple[WriteAuthorizationRecord, ...]: ...

    def revoke(
        self,
        *,
        authorization_id: str,
        company_id: int,
        revoked_by: str,
        review_id: str,
    ) -> WriteAuthorizationRecord: ...

    def claim_and_consume(
        self,
        *,
        company_id: int,
        review_id: str,
        operation_type: WriteAuthorizationOperationType,
        target_version: int,
        authorization_id: str,
        trace_id: str | None = None,
        execution_id: str,
    ) -> WriteAuthorizationRecord:
        """Flush consumption under a row lock held until the application commits.

        A consumed authorization permits only same-execution recovery, within TTL.
        Crash before the outer commit rolls consumption back with runtime state.
        """
        ...
