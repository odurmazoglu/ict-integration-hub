"""P0-PROD-12A: read-only operator execution/recovery status use case.

Proves the endpoint's application-layer composition (GetWorkbenchExecutionStatusUseCase)
handles every review state cleanly (no decision, decision but no execution, waiting_retry,
completed), exposes exactly the persisted retry/failure/artifact/evidence/authorization
facts an operator needs to diagnose the real P0-PROD-10C/10G incident shape without SSH/
psql, never fabricates an authorization relationship the data cannot prove, and performs
zero persistence/write side effects.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.execution.contracts import (
    AcceptedReviewDecision,
    ExecutionArtifact,
    ExecutionArtifactType,
    ExecutionMode,
    ExecutionPlan,
    ExecutionSourceInvoice,
    ExecutionStep,
    ExecutionStepResult,
    ExecutionStepStatus,
    ExecutionStepType,
)
from app.application.execution.exceptions import ExecutionSourceInvoiceNotFoundError
from app.application.execution.runtime import (
    ExecutionCheckpoint,
    ExecutionFailure,
    ExecutionRetryPolicy,
    ExecutionRetryPolicyType,
    ExecutionRuntimeStep,
    ExecutionRuntimeStepState,
    ExecutionSnapshot,
    ExecutionState,
)
from app.application.workbench.dto import ReviewItem, ReviewStatus
from app.application.workbench.exceptions import ReviewNotFoundError
from app.application.workbench.execution_status_use_cases import GetWorkbenchExecutionStatusUseCase
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.write_authorization import (
    WriteAuthorizationOperationType,
    WriteAuthorizationRecord,
    WriteAuthorizationStatus,
)
from app.application.workflow import WorkflowType

COMPANY_ID = 1
OTHER_COMPANY_ID = 2
REVIEW_ID = "review:1316ab15-4bcb-522c-b65a-e5785ee106e0"
DECISION_ID = "review-decision:1699d6ca-c1df-4450-a94c-7171b77cba81"
DECISION_VERSION = 3
EXECUTION_ID = "accepted-decision-execution:e20d3b73-05f3-5f63-be01-205fe8dac169"
VENDOR_BILL_STEP_KEY = f"{REVIEW_ID}:{DECISION_VERSION}:vendor_bill:workflow"


# --------------------------------------------------------------------------- fakes


class _FakeReviewReader:
    def __init__(self, item: ReviewItem | None, *, company_id: int = COMPANY_ID) -> None:
        self._item = item
        self._company_id = company_id

    def get_review_item(self, query: ReviewDetailQuery) -> ReviewItem:
        if self._item is None or self._item.review_id != query.review_id or self._company_id != query.company_id:
            raise ReviewNotFoundError("Review was not found in this company scope.")
        return self._item

    def list_review_items(self, query):  # pragma: no cover - unused by this use case
        raise NotImplementedError


class _StaticAcceptedDecisionReader:
    def __init__(self, decision: AcceptedReviewDecision | None) -> None:
        self._decision = decision
        self.calls: list[tuple[str, int, int]] = []

    def get_accepted_decision(
        self, *, review_id: str, company_id: int, decision_version: int
    ) -> AcceptedReviewDecision:
        self.calls.append((review_id, company_id, decision_version))
        if self._decision is None or self._decision.decision_version != decision_version:
            raise ReviewNotFoundError("Accepted review decision was not found.")
        return self._decision


class _StaticExecutionSnapshotReader:
    def __init__(self, snapshot: ExecutionSnapshot | None) -> None:
        self._snapshot = snapshot
        self.calls: list[tuple[str, int]] = []

    def find_latest_snapshot_for_review(self, *, review_id: str, company_id: int) -> ExecutionSnapshot | None:
        self.calls.append((review_id, company_id))
        return self._snapshot


class _StaticEvidencePresenceReader:
    """Shared fake for both Stage-1 (get_evidence) and Stage-2 (get_source_invoice)."""

    def __init__(self, *, present_at: int | None) -> None:
        self._present_at = present_at
        self.calls: list[int] = []

    def get_evidence(self, *, review_id: str, company_id: int, expected_version: int) -> ExecutionSourceInvoice:
        self.calls.append(expected_version)
        if self._present_at != expected_version:
            raise ExecutionSourceInvoiceNotFoundError("Execution source evidence was not found.")
        return object()  # type: ignore[return-value]

    def get_source_invoice(self, *, review_id: str, company_id: int, decision_version: int) -> ExecutionSourceInvoice:
        self.calls.append(decision_version)
        if self._present_at != decision_version:
            raise ExecutionSourceInvoiceNotFoundError("Execution source evidence was not found.")
        return object()  # type: ignore[return-value]


class _StaticWriteAuthorizationRepository:
    def __init__(self, records: tuple[WriteAuthorizationRecord, ...] = ()) -> None:
        self._records = records
        self.calls: list[tuple[str, int]] = []

    def list_for_review(self, *, review_id: str, company_id: int) -> tuple[WriteAuthorizationRecord, ...]:
        self.calls.append((review_id, company_id))
        return self._records


# --------------------------------------------------------------------------- builders


def _review(*, version: int = DECISION_VERSION, status: ReviewStatus = ReviewStatus.DECISION_SUBMITTED) -> ReviewItem:
    return ReviewItem(
        review_id=REVIEW_ID,
        invoice_id="F1ADCCAD-FB70-9EF1-8105-005056BB160F",
        invoice_number="HD12026000964602",
        supplier_tax_number="2650179910",
        supplier_name="D-MARKET ELEKTRONIK HIZMETLER VE TICARET ANONIM SIRKETI",
        invoice_date=date(2026, 9, 10),
        currency="TRY",
        total_amount=Decimal("2599.20"),
        workflow=WorkflowType.VENDOR_BILL,
        status=status,
        version=version,
    )


def _decision() -> AcceptedReviewDecision:
    return AcceptedReviewDecision(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
        decision_id=DECISION_ID,
        selected_workflow=WorkflowType.VENDOR_BILL,
    )


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        execution_id=EXECUTION_ID,
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
        mode=ExecutionMode.EXECUTE,
        decision_id=DECISION_ID,
        steps=(
            ExecutionStep(
                step_key=VENDOR_BILL_STEP_KEY,
                step_type=ExecutionStepType.VENDOR_BILL,
                allocation_keys=(),
                sequence=1,
                execute_supported=True,
                writer_required=True,
            ),
        ),
    )


def _pilot_snapshot(
    *,
    state: ExecutionState,
    retry_count: int,
    max_attempts: int = 2,
    failure: ExecutionFailure | None = None,
    last_result: ExecutionStepResult | None = None,
) -> ExecutionSnapshot:
    return ExecutionSnapshot(
        execution_id=EXECUTION_ID,
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
        mode=ExecutionMode.EXECUTE,
        state=state,
        idempotency_key="vendor-bill-write:c86c866fd5c7340bddf808ebffd880c94a09bca45fc6e9e216c13586a355e1db",
        plan=_plan(),
        steps=(
            ExecutionRuntimeStep(
                step_key=VENDOR_BILL_STEP_KEY,
                step_type=ExecutionStepType.VENDOR_BILL,
                sequence=1,
                state=(
                    ExecutionRuntimeStepState.WAITING_RETRY
                    if state is ExecutionState.WAITING_RETRY
                    else ExecutionRuntimeStepState.COMPLETED
                    if state is ExecutionState.COMPLETED
                    else ExecutionRuntimeStepState.RUNNING
                ),
                allocation_keys=(),
                retry_count=retry_count,
                last_result=last_result,
            ),
        ),
        checkpoint=ExecutionCheckpoint(
            execution_id=EXECUTION_ID,
            completed_step_keys=(VENDOR_BILL_STEP_KEY,) if state is ExecutionState.COMPLETED else (),
            failed_step_key=VENDOR_BILL_STEP_KEY if failure is not None else None,
            current_step_key=None if state is ExecutionState.COMPLETED else VENDOR_BILL_STEP_KEY,
            retry_count=retry_count,
            last_event_id=None,
        ),
        retry_policy=ExecutionRetryPolicy(policy_type=ExecutionRetryPolicyType.IMMEDIATE, max_attempts=max_attempts),
        failure=failure,
    )


def _c62_failure() -> ExecutionFailure:
    return ExecutionFailure(
        step_key=VENDOR_BILL_STEP_KEY,
        error_code="vendor_bill_write_error",
        safe_message=(
            'Odoo returned HTTP 500. invalid input syntax for type integer: "C62" '
            "LINE 1: ... Iceflow Flip Straw 2.0 Pipet', 448, 2166.0, 389, 'C62', '1...."
        ),
    )


def _authorization(
    *,
    authorization_id: str,
    created_at: datetime,
    status: WriteAuthorizationStatus = WriteAuthorizationStatus.CONSUMED,
    consumed_by_execution_id: str | None = EXECUTION_ID,
    expires_at: datetime | None = None,
    company_id: int = COMPANY_ID,
) -> WriteAuthorizationRecord:
    return WriteAuthorizationRecord(
        authorization_id=authorization_id,
        company_id=company_id,
        review_id=REVIEW_ID,
        operation_type=WriteAuthorizationOperationType.EXECUTE_VENDOR_BILL,
        target_version=DECISION_VERSION,
        status=status,
        authorized_by="p0-prod-operator",
        created_at=created_at,
        expires_at=expires_at or (created_at + timedelta(minutes=15)),
        consumed_at=created_at if consumed_by_execution_id is not None else None,
        consumed_by_execution_id=consumed_by_execution_id,
        use_count=1 if consumed_by_execution_id is not None else 0,
    )


def _use_case(
    *,
    review: ReviewItem | None,
    decision: AcceptedReviewDecision | None = None,
    snapshot: ExecutionSnapshot | None = None,
    stage_one_present_at: int | None = None,
    stage_two_present_at: int | None = None,
    authorizations: tuple[WriteAuthorizationRecord, ...] = (),
) -> tuple[GetWorkbenchExecutionStatusUseCase, dict]:
    fakes = {
        "review": _FakeReviewReader(review),
        "decision": _StaticAcceptedDecisionReader(decision),
        "snapshot": _StaticExecutionSnapshotReader(snapshot),
        "stage_one": _StaticEvidencePresenceReader(present_at=stage_one_present_at),
        "stage_two": _StaticEvidencePresenceReader(present_at=stage_two_present_at),
        "authorizations": _StaticWriteAuthorizationRepository(authorizations),
    }
    use_case = GetWorkbenchExecutionStatusUseCase(
        review_reader=fakes["review"],
        accepted_decision_reader=fakes["decision"],
        execution_snapshot_reader=fakes["snapshot"],
        stage_one_evidence_reader=fakes["stage_one"],
        stage_two_evidence_reader=fakes["stage_two"],
        write_authorization_repository=fakes["authorizations"],
    )
    return use_case, fakes


# --------------------------------------------------------------------------- tests


def test_unknown_review_fails_closed() -> None:
    """1. review not found fails closed."""

    use_case, _ = _use_case(review=None)
    with pytest.raises(ReviewNotFoundError):
        use_case.execute(review_id=REVIEW_ID, company_id=COMPANY_ID)


def test_wrong_company_fails_closed() -> None:
    """1. wrong-company review lookup fails closed exactly like every other Workbench read."""

    use_case, fakes = _use_case(review=_review())
    with pytest.raises(ReviewNotFoundError):
        use_case.execute(review_id=REVIEW_ID, company_id=OTHER_COMPANY_ID)


def test_review_exists_with_no_decision_and_no_execution() -> None:
    """2. review exists but no decision/execution -- not an API error, execution=None."""

    review = _review(version=1, status=ReviewStatus.PENDING_REVIEW)
    use_case, _ = _use_case(review=review, decision=None, snapshot=None)

    status = use_case.execute(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert status.review_id == REVIEW_ID
    assert status.review_version == 1
    assert status.review_status is ReviewStatus.PENDING_REVIEW
    assert status.decision is None
    assert status.execution is None
    assert status.failure is None
    assert status.artifacts == ()
    assert status.authorization is None
    assert status.recovery.execution_completed is False
    assert status.recovery.waiting_retry is False
    assert status.recovery.remaining_attempts == 0


def test_decision_exists_but_execution_has_not_started() -> None:
    """3. decision exists, execution has not started -- decision populated, execution=None."""

    use_case, _ = _use_case(review=_review(), decision=_decision(), snapshot=None)

    status = use_case.execute(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert status.decision is not None
    assert status.decision.decision_id == DECISION_ID
    assert status.decision.decision_version == DECISION_VERSION
    assert status.decision.selected_workflow is WorkflowType.VENDOR_BILL
    assert status.execution is None
    assert status.recovery.waiting_retry is False


def test_waiting_retry_exposes_retry_count_max_attempts_and_remaining() -> None:
    """4. waiting_retry execution exposes retry_count/max_attempts/remaining_attempts."""

    snapshot = _pilot_snapshot(
        state=ExecutionState.WAITING_RETRY,
        retry_count=1,
        max_attempts=2,
        failure=_c62_failure(),
    )
    use_case, _ = _use_case(review=_review(), decision=_decision(), snapshot=snapshot)

    status = use_case.execute(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert status.execution is not None
    assert status.execution.execution_id == EXECUTION_ID
    assert status.execution.mode is ExecutionMode.EXECUTE
    assert status.execution.state is ExecutionState.WAITING_RETRY
    assert status.execution.retry_count == 1
    assert status.execution.max_attempts == 2
    assert status.execution.remaining_attempts == 1
    assert status.execution.retry_possible is True
    assert status.recovery.waiting_retry is True
    assert status.recovery.execution_completed is False
    assert status.recovery.remaining_attempts == 1


def test_waiting_retry_exhausted_reports_no_remaining_attempts_and_not_retry_possible() -> None:
    """4b. retry_count == max_attempts leaves zero remaining attempts and retry_possible=False."""

    snapshot = _pilot_snapshot(
        state=ExecutionState.WAITING_RETRY,
        retry_count=2,
        max_attempts=2,
        failure=_c62_failure(),
    )
    use_case, _ = _use_case(review=_review(), decision=_decision(), snapshot=snapshot)

    status = use_case.execute(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert status.execution.remaining_attempts == 0
    assert status.execution.retry_possible is False


def test_waiting_retry_exposes_prior_failure_code_and_message() -> None:
    """5. waiting_retry exposes the exact persisted prior failure code/message."""

    snapshot = _pilot_snapshot(state=ExecutionState.WAITING_RETRY, retry_count=1, failure=_c62_failure())
    use_case, _ = _use_case(review=_review(), decision=_decision(), snapshot=snapshot)

    status = use_case.execute(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert status.failure is not None
    assert status.failure.step_key == VENDOR_BILL_STEP_KEY
    assert status.failure.error_code == "vendor_bill_write_error"
    assert 'invalid input syntax for type integer: "C62"' in status.failure.safe_message


def test_completed_execution_exposes_exactly_the_persisted_vendor_bill_artifact() -> None:
    """6. completed execution exposes exactly the persisted Vendor Bill artifact id."""

    artifact = ExecutionArtifact(
        artifact_type=ExecutionArtifactType.VENDOR_BILL,
        artifact_id="62",
        external_identity="vendor-bill-write:c86c866fd5c7340bddf808ebffd880c94a09bca45fc6e9e216c13586a355e1db",
        created=True,
    )
    last_result = ExecutionStepResult(
        step_key=VENDOR_BILL_STEP_KEY,
        step_type=ExecutionStepType.VENDOR_BILL,
        status=ExecutionStepStatus.EXECUTED,
        dry_run=False,
        message="Draft Vendor Bill created in Odoo.",
        produced_artifacts=(artifact,),
    )
    snapshot = _pilot_snapshot(state=ExecutionState.COMPLETED, retry_count=1, last_result=last_result)
    use_case, _ = _use_case(review=_review(), decision=_decision(), snapshot=snapshot)

    status = use_case.execute(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert status.execution.state is ExecutionState.COMPLETED
    assert status.recovery.execution_completed is True
    assert status.recovery.waiting_retry is False
    assert status.failure is None
    assert len(status.artifacts) == 1
    assert status.artifacts[0].artifact_type is ExecutionArtifactType.VENDOR_BILL
    assert status.artifacts[0].artifact_id == "62"
    assert status.artifacts[0].created is True


def test_stage_one_and_stage_two_evidence_metadata_is_exposed_correctly() -> None:
    """7. Stage-1/Stage-2 evidence presence/version metadata is exposed correctly."""

    # decision_version=3 -> stage-1 review_version_before = 2.
    use_case, fakes = _use_case(
        review=_review(),
        decision=_decision(),
        snapshot=None,
        stage_one_present_at=2,
        stage_two_present_at=3,
    )

    status = use_case.execute(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert status.evidence.stage_one_present is True
    assert status.evidence.stage_one_review_version == 2
    assert status.evidence.stage_two_present is True
    assert status.evidence.stage_two_decision_version == 3
    assert fakes["stage_one"].calls == [2]
    assert fakes["stage_two"].calls == [3]


def test_missing_stage_evidence_reports_absent_not_an_error() -> None:
    """7b. absent Stage-1/Stage-2 evidence is reported as present=False, never raised."""

    use_case, _ = _use_case(
        review=_review(),
        decision=_decision(),
        snapshot=None,
        stage_one_present_at=None,
        stage_two_present_at=None,
    )

    status = use_case.execute(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert status.evidence.stage_one_present is False
    assert status.evidence.stage_one_review_version is None
    assert status.evidence.stage_two_present is False
    assert status.evidence.stage_two_decision_version is None


def test_authorization_exposed_only_when_provably_consumed_by_this_execution() -> None:
    """8. authorization relationship exposed only when provable via consumed_by_execution_id."""

    now = datetime.now(UTC)
    matching = _authorization(authorization_id="dd0927e8-9b0a-4f2b-8cfb-98d8de5ee343", created_at=now)
    unrelated = _authorization(
        authorization_id="00000000-0000-0000-0000-00000000ffff",
        created_at=now - timedelta(hours=1),
        consumed_by_execution_id="some-other-execution",
    )
    snapshot = _pilot_snapshot(state=ExecutionState.COMPLETED, retry_count=0)
    use_case, _ = _use_case(
        review=_review(),
        decision=_decision(),
        snapshot=snapshot,
        authorizations=(matching, unrelated),
    )

    status = use_case.execute(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert status.authorization is not None
    assert status.authorization.authorization_id == "dd0927e8-9b0a-4f2b-8cfb-98d8de5ee343"
    assert status.authorization.consumed_by_execution_id == EXECUTION_ID


def test_authorization_is_none_when_no_persisted_record_proves_the_relationship() -> None:
    """8b. do NOT fabricate a relationship -- a pending, not-yet-consumed authorization
    for the same review/version is deliberately NOT surfaced as "the" execution's
    authorization, since the schema cannot prove that link."""

    pending = _authorization(
        authorization_id="00000000-0000-0000-0000-0000000000aa",
        created_at=datetime.now(UTC),
        status=WriteAuthorizationStatus.PENDING,
        consumed_by_execution_id=None,
    )
    snapshot = _pilot_snapshot(state=ExecutionState.WAITING_RETRY, retry_count=1, failure=_c62_failure())
    use_case, _ = _use_case(review=_review(), decision=_decision(), snapshot=snapshot, authorizations=(pending,))

    status = use_case.execute(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert status.authorization is None


def test_most_recent_authorization_wins_when_two_prove_the_same_execution() -> None:
    """8c. P0-PROD-10G shape: a fresh authorization consumed the same execution_id after
    the original one expired -- the most recently created provable match must be
    reported, not an arbitrary one."""

    now = datetime.now(UTC)
    stale = _authorization(
        authorization_id="fdbaea80-7f31-46fb-ab0e-8b53e70f3913",
        created_at=now - timedelta(days=1),
    )
    fresh = _authorization(
        authorization_id="dd0927e8-9b0a-4f2b-8cfb-98d8de5ee343",
        created_at=now,
    )
    # list_for_review is documented (and here faked) as created_at desc, matching
    # the real repository's ordering.
    snapshot = _pilot_snapshot(state=ExecutionState.COMPLETED, retry_count=1)
    use_case, _ = _use_case(review=_review(), decision=_decision(), snapshot=snapshot, authorizations=(fresh, stale))

    status = use_case.execute(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert status.authorization is not None
    assert status.authorization.authorization_id == "dd0927e8-9b0a-4f2b-8cfb-98d8de5ee343"


def test_expired_authorization_reports_computed_is_expired_correctly() -> None:
    """9. expired authorization reports computed is_expired correctly, not a stale DB status."""

    now = datetime.now(UTC)
    expired = _authorization(
        authorization_id="fdbaea80-7f31-46fb-ab0e-8b53e70f3913",
        created_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1, minutes=45),
    )
    snapshot = _pilot_snapshot(state=ExecutionState.WAITING_RETRY, retry_count=1, failure=_c62_failure())
    use_case, _ = _use_case(review=_review(), decision=_decision(), snapshot=snapshot, authorizations=(expired,))

    status = use_case.execute(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert status.authorization is not None
    # The DB `status` column can lag (never server-side transitioned to EXPIRED),
    # but `is_expired` is always freshly computed from `expires_at`.
    assert status.authorization.status is WriteAuthorizationStatus.CONSUMED
    assert status.authorization.is_expired is True


def test_cross_company_isolation_no_leak_via_review_lookup() -> None:
    """10. cross-company isolation: a review scoped to company 1 is invisible to company 2,
    and no evidence/execution/authorization read is even attempted."""

    snapshot = _pilot_snapshot(state=ExecutionState.WAITING_RETRY, retry_count=1, failure=_c62_failure())
    use_case, fakes = _use_case(
        review=_review(),
        decision=_decision(),
        snapshot=snapshot,
        authorizations=(
            _authorization(authorization_id="00000000-0000-0000-0000-0000000000bb", created_at=datetime.now(UTC)),
        ),
    )

    with pytest.raises(ReviewNotFoundError):
        use_case.execute(review_id=REVIEW_ID, company_id=OTHER_COMPANY_ID)

    assert fakes["decision"].calls == []
    assert fakes["snapshot"].calls == []
    assert fakes["stage_one"].calls == []
    assert fakes["stage_two"].calls == []
    assert fakes["authorizations"].calls == []


def test_use_case_performs_no_persistence_or_write_side_effects() -> None:
    """11. the endpoint/use case performs zero writes -- every fake reader here exposes
    only read methods; none has a create/update/delete/commit method at all, so any
    attempted write would be an AttributeError, not a silent no-op."""

    snapshot = _pilot_snapshot(state=ExecutionState.COMPLETED, retry_count=0)
    use_case, fakes = _use_case(review=_review(), decision=_decision(), snapshot=snapshot)

    for fake in fakes.values():
        for attr in ("create", "update", "delete", "commit", "save", "claim_and_consume", "persist_transition"):
            assert not hasattr(fake, attr), f"{fake!r} unexpectedly exposes write method {attr!r}"

    use_case.execute(review_id=REVIEW_ID, company_id=COMPANY_ID)


def test_review_reasons_and_status_reflect_persisted_review_item() -> None:
    """Sanity: top-level review fields are read straight from the persisted ReviewItem."""

    review = _review(version=DECISION_VERSION, status=ReviewStatus.DECISION_SUBMITTED)
    use_case, _ = _use_case(review=review, decision=_decision(), snapshot=None)

    status = use_case.execute(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert status.review_version == DECISION_VERSION
    assert status.review_status is ReviewStatus.DECISION_SUBMITTED
    assert status.company_id == COMPANY_ID
