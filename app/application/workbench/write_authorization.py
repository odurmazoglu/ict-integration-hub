from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from app.application.dto import ApplicationDTO
from app.application.exceptions import ApplicationError


class WriteAuthorizationOperationType(StrEnum):
    """Permitted operation types for narrow write authorizations."""

    EXECUTE_VENDOR_BILL = "EXECUTE_VENDOR_BILL"
    #: P0-PROD-09F. Authorizes one CREATE_PERMANENT_SUPPLIER resolution write.
    CREATE_PERMANENT_SUPPLIER = "CREATE_PERMANENT_SUPPLIER"
    #: P0-PROD-09F. Authorizes one ONE_OFF_VENDOR resolution write -- covers both a
    #: genuinely new partner create and reuse of an archived Hub-owned partner (#159):
    #: both go through the exact same OdooSupplierPartnerWriter.create_supplier call.
    ONE_OFF_VENDOR_SUPPLIER = "ONE_OFF_VENDOR_SUPPLIER"
    #: P0-PROD-09F. Authorizes one explicit ONE_OFF_VENDOR archive/recovery write
    #: (the #162 recovery endpoint). Never used by the automatic post-execution
    #: retirement trigger, which remains gated by the existing global flag only.
    ONE_OFF_VENDOR_ARCHIVE = "ONE_OFF_VENDOR_ARCHIVE"
    #: P0-PROD-09G. Authorizes one CREATE_NEW_PRODUCT remediation write -- covers
    #: both the product.template create and the product.supplierinfo create/link
    #: that follows it for the same review line (CreateNewProductUseCase claims it
    #: again, idempotently, immediately before each of the two Odoo calls).
    CREATE_NEW_PRODUCT = "CREATE_NEW_PRODUCT"


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
        self.ensure_scope(
            company_id=company_id,
            review_id=review_id,
            operation_type=WriteAuthorizationOperationType.EXECUTE_VENDOR_BILL,
            target_version=decision_version,
            consumer_id=execution_id,
            scope_error_message="Authorization does not match the exact Vendor Bill execution scope.",
        )

    def ensure_scope(
        self,
        *,
        company_id: int,
        review_id: str,
        operation_type: WriteAuthorizationOperationType,
        target_version: int,
        consumer_id: str,
        scope_error_message: str = "Authorization does not match the exact requested write scope.",
    ) -> None:
        """General-purpose defense-in-depth re-check for any operation type (P0-PROD-09F).

        ``ensure_execution_scope`` above is now a thin, behavior-preserving wrapper
        over this -- every existing Vendor Bill execution call site and test is
        unaffected. ``consumer_id`` generalizes ``execution_id``: the opaque,
        deterministic identity of the *specific write attempt* that claimed this
        authorization (see each operation's own consumer-id helper), so a legitimate
        retry of the same attempt can still use its own already-consumed
        authorization while an unrelated attempt cannot.
        """

        if (self.company_id, self.review_id, self.operation_type, self.target_version) != (
            company_id,
            review_id,
            operation_type,
            target_version,
        ):
            raise WriteAuthorizationScopeMismatchError(scope_error_message)
        if self.status is not WriteAuthorizationStatus.CONSUMED or self.consumed_by_execution_id != consumer_id:
            raise WriteAuthorizationAlreadyConsumedError("Authorization must be bound to this write attempt.")
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


def supplier_resolution_authorization_consumer_id(
    *, company_id: int, review_id: str, expected_version: int, mode: str
) -> str:
    """Deterministic consumer identity for a CREATE_PERMANENT_SUPPLIER/ONE_OFF_VENDOR_SUPPLIER
    write attempt (P0-PROD-09F). Mirrors ``accepted_decision_execution_id``'s exact
    construction: same inputs always produce the same id, so a legitimate crash-then-
    retry of the same ``ResolveWorkbenchSupplierCommand`` resumes against its own
    already-consumed authorization instead of being rejected as a different attempt.
    """

    identity = f"supplier-resolution-write:{company_id}:{review_id}:{expected_version}:{mode}"
    return f"supplier-resolution-write:{uuid5(NAMESPACE_URL, identity)}"


def one_off_vendor_archive_authorization_consumer_id(*, company_id: int, review_id: str, review_version: int) -> str:
    """Deterministic consumer identity for a ONE_OFF_VENDOR_ARCHIVE recovery write
    attempt (P0-PROD-09F). Same construction discipline as
    ``supplier_resolution_authorization_consumer_id`` -- a retry of the same recovery
    request resumes against its own already-consumed authorization."""

    identity = f"one-off-vendor-archive-write:{company_id}:{review_id}:{review_version}"
    return f"one-off-vendor-archive-write:{uuid5(NAMESPACE_URL, identity)}"


def product_remediation_authorization_consumer_id(
    *, company_id: int, review_id: str, expected_version: int, line_number: str, categ_id: int | None = None
) -> str:
    """Deterministic consumer identity for a CREATE_NEW_PRODUCT write attempt
    (P0-PROD-09G). Same construction discipline as the other consumer-id helpers --
    keyed by ``line_number`` (not by which of the two underlying Odoo writes is in
    flight), so ``CreateNewProductUseCase`` can claim the same authorization again,
    idempotently, immediately before both the product.template create and the
    product.supplierinfo create/link, and a legitimate crash-then-retry of either
    step resumes against its own already-consumed authorization.

    P0-PROD-18E-2: a reserved ``categ_id`` is bound into the identity, so an
    authorization consumed for one category can never authorize a write in another.
    Without a category the identity is byte-identical to before (existing consumed
    authorizations keep resuming).
    """

    identity = f"product-remediation-write:{company_id}:{review_id}:{expected_version}:{line_number}"
    if categ_id is not None:
        identity = f"{identity}:categ:{categ_id}"
    return f"product-remediation-write:{uuid5(NAMESPACE_URL, identity)}"
