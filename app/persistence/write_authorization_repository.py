from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.workbench.write_authorization import (
    WriteAuthorizationAlreadyConsumedError,
    WriteAuthorizationError,
    WriteAuthorizationExpiredError,
    WriteAuthorizationNotFoundError,
    WriteAuthorizationOperationType,
    WriteAuthorizationRecord,
    WriteAuthorizationRevokedError,
    WriteAuthorizationScopeMismatchError,
    WriteAuthorizationStatus,
)
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_one_off_vendor_retirement import WorkbenchReviewOneOffVendorRetirement
from app.models.workbench_review_write_authorization import WorkbenchReviewWriteAuthorization


def _record_from_model(model: WorkbenchReviewWriteAuthorization) -> WriteAuthorizationRecord:
    status = WriteAuthorizationStatus(model.status)
    if status is WriteAuthorizationStatus.PENDING and datetime.now(UTC) >= model.expires_at:
        status = WriteAuthorizationStatus.EXPIRED

    return WriteAuthorizationRecord(
        authorization_id=model.authorization_id,
        company_id=model.company_id,
        review_id=model.review_id,
        operation_type=WriteAuthorizationOperationType(model.operation_type),
        target_version=model.target_version,
        status=status,
        authorized_by=model.authorized_by,
        created_at=model.created_at,
        expires_at=model.expires_at,
        justification=model.justification,
        consumed_at=model.consumed_at,
        consumed_by_trace_id=model.consumed_by_trace_id,
        consumed_by_execution_id=model.consumed_by_execution_id,
        revoked_at=model.revoked_at,
        revoked_by=model.revoked_by,
        use_count=model.use_count,
        last_used_at=model.last_used_at,
        last_used_trace_id=model.last_used_trace_id,
    )


class SqlAlchemyWriteAuthorizationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

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
    ) -> WriteAuthorizationRecord:
        model = WorkbenchReviewWriteAuthorization(
            authorization_id=authorization_id,
            company_id=company_id,
            review_id=review_id,
            operation_type=operation_type.value,
            target_version=target_version,
            status="pending",
            authorized_by=authorized_by,
            expires_at=expires_at,
            justification=justification,
        )
        try:
            self._session.add(model)
            self._session.flush()
            self._session.refresh(model)
            return _record_from_model(model)
        except SQLAlchemyError as exc:
            raise WriteAuthorizationError("Failed to persist write authorization.") from exc

    def get_by_id(
        self,
        *,
        authorization_id: str,
        company_id: int,
    ) -> WriteAuthorizationRecord | None:
        model = self._session.scalar(
            select(WorkbenchReviewWriteAuthorization).where(
                WorkbenchReviewWriteAuthorization.authorization_id == authorization_id,
                WorkbenchReviewWriteAuthorization.company_id == company_id,
            )
        )
        return _record_from_model(model) if model is not None else None

    def list_for_review(
        self,
        *,
        review_id: str,
        company_id: int,
    ) -> tuple[WriteAuthorizationRecord, ...]:
        models = self._session.scalars(
            select(WorkbenchReviewWriteAuthorization)
            .where(
                WorkbenchReviewWriteAuthorization.review_id == review_id,
                WorkbenchReviewWriteAuthorization.company_id == company_id,
            )
            .order_by(WorkbenchReviewWriteAuthorization.created_at.desc())
        ).all()
        return tuple(_record_from_model(m) for m in models)

    def revoke(
        self,
        *,
        authorization_id: str,
        company_id: int,
        revoked_by: str,
        review_id: str,
    ) -> WriteAuthorizationRecord:
        model = self._session.scalar(
            select(WorkbenchReviewWriteAuthorization)
            .where(
                WorkbenchReviewWriteAuthorization.authorization_id == authorization_id,
                WorkbenchReviewWriteAuthorization.company_id == company_id,
                WorkbenchReviewWriteAuthorization.review_id == review_id,
            )
            .with_for_update()
        )
        if model is None:
            raise WriteAuthorizationNotFoundError("Write authorization was not found.")
        if model.status == "revoked":
            return _record_from_model(model)

        now = datetime.now(UTC)
        if now >= model.expires_at:
            raise WriteAuthorizationExpiredError("Expired authorization cannot be revoked.")

        model.status = "revoked"
        model.revoked_at = now
        model.revoked_by = revoked_by
        self._session.flush()
        self._session.refresh(model)
        return _record_from_model(model)

    def claim_and_consume(
        self,
        *,
        company_id: int,
        review_id: str,
        operation_type: WriteAuthorizationOperationType,
        target_version: int,
        authorization_id: str,
        execution_id: str,
        trace_id: str | None = None,
    ) -> WriteAuthorizationRecord:
        if not authorization_id or not execution_id:
            raise WriteAuthorizationScopeMismatchError("Explicit authorization and execution identities are required.")
        try:
            model = self._session.scalar(
                select(WorkbenchReviewWriteAuthorization)
                .where(
                    WorkbenchReviewWriteAuthorization.authorization_id == authorization_id,
                    WorkbenchReviewWriteAuthorization.company_id == company_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if model is None:
                raise WriteAuthorizationNotFoundError("Specified write authorization was not found.")
            if (model.review_id, model.operation_type, model.target_version) != (
                review_id,
                operation_type.value,
                target_version,
            ):
                raise WriteAuthorizationScopeMismatchError("Write authorization scope does not match request.")
            self._ensure_target_still_valid(
                operation_type=operation_type,
                review_id=review_id,
                company_id=company_id,
                target_version=target_version,
            )
            now = datetime.now(UTC)
            if model.status == "revoked":
                raise WriteAuthorizationRevokedError("Write authorization has been revoked.")
            if now >= model.expires_at:
                raise WriteAuthorizationExpiredError("Write authorization has expired.")
            if model.status == "consumed" and model.consumed_by_execution_id != execution_id:
                raise WriteAuthorizationAlreadyConsumedError("Authorization belongs to a different execution.")
            if model.status not in {"pending", "consumed"}:
                raise WriteAuthorizationError("Write authorization has an invalid lifecycle state.")
            # FOR UPDATE serializes PostgreSQL callers until application commit.
            # The conditional update also rejects stale claims on adapters without row locks.
            result = self._session.execute(
                update(WorkbenchReviewWriteAuthorization)
                .where(
                    WorkbenchReviewWriteAuthorization.id == model.id,
                    WorkbenchReviewWriteAuthorization.status == model.status,
                    WorkbenchReviewWriteAuthorization.use_count == model.use_count,
                )
                .values(
                    status="consumed",
                    consumed_at=model.consumed_at or now,
                    consumed_by_execution_id=execution_id,
                    consumed_by_trace_id=model.consumed_by_trace_id or trace_id,
                    use_count=model.use_count + 1,
                    last_used_at=now,
                    last_used_trace_id=trace_id,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise WriteAuthorizationAlreadyConsumedError("Authorization was changed by a concurrent request.")
            self._session.flush()
            self._session.refresh(model)
            return _record_from_model(model)
        except SQLAlchemyError as exc:
            raise WriteAuthorizationError("Failed to claim write authorization.") from exc

    def _ensure_target_still_valid(
        self,
        *,
        operation_type: WriteAuthorizationOperationType,
        review_id: str,
        company_id: int,
        target_version: int,
    ) -> None:
        """P0-PROD-09F: what "the target is still valid" means depends on the
        operation. EXECUTE_VENDOR_BILL/CREATE_PERMANENT_SUPPLIER/ONE_OFF_VENDOR_SUPPLIER
        all target the review's *current* version -- unchanged from 09D1.
        ONE_OFF_VENDOR_ARCHIVE targets a specific, already-persisted retirement row's
        own version, which by design is almost always behind the review's current
        version by the time recovery is needed (the review keeps advancing through
        decision submission and execution after the retirement row is created) -- so
        it is validated against that row's continued existence instead.
        """

        if operation_type is WriteAuthorizationOperationType.ONE_OFF_VENDOR_ARCHIVE:
            retirement_exists = self._session.scalar(
                select(WorkbenchReviewOneOffVendorRetirement.id).where(
                    WorkbenchReviewOneOffVendorRetirement.review_id == review_id,
                    WorkbenchReviewOneOffVendorRetirement.company_id == company_id,
                    WorkbenchReviewOneOffVendorRetirement.review_version == target_version,
                )
            )
            if retirement_exists is None:
                raise WriteAuthorizationScopeMismatchError(
                    "Authorization targets a retirement row that no longer exists."
                )
            return
        current_version = self._session.scalar(
            select(WorkbenchReviewItem.version).where(
                WorkbenchReviewItem.review_id == review_id,
                WorkbenchReviewItem.company_id == company_id,
            )
        )
        if current_version != target_version:
            raise WriteAuthorizationScopeMismatchError("Authorization targets a stale review version.")
