"""SqlAlchemyVendorBillExecutionEvidenceReader (P0-PROD-08H).

Proves the reader answers "durable, successful Vendor Bill exists?" purely from
the EXISTING workflow_executions/workflow_execution_steps persistence -- no new
tracking table, matching the design in one_off_vendor_retirement.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.models.workflow_execution import WorkflowExecution, WorkflowExecutionStep
from app.persistence import SqlAlchemyVendorBillExecutionEvidenceReader

COMPANY_ID = 7
REVIEW_ID = "review:vb-evidence-1"


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[WorkflowExecution.__table__, WorkflowExecutionStep.__table__],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


def _execution(session: Session, *, execution_id: str, review_id: str = REVIEW_ID) -> None:
    session.add(
        WorkflowExecution(
            execution_id=execution_id,
            review_id=review_id,
            decision_version=2,
            company_id=COMPANY_ID,
            state="executing",
            mode="execute",
            idempotency_key=f"idem:{execution_id}",
            plan_signature="sig",
            plan={},
            checkpoint={},
            retry_policy={},
        )
    )
    session.flush()


def _step(
    session: Session,
    *,
    execution_id: str,
    step_type: str = "vendor_bill",
    state: str = "completed",
    sequence: int = 1,
) -> None:
    session.add(
        WorkflowExecutionStep(
            execution_id=execution_id,
            step_key=f"{execution_id}:{sequence}:{step_type}",
            step_type=step_type,
            sequence=sequence,
            state=state,
            allocation_keys=[],
            retry_count=0,
            last_result={
                "status": "executed",
                "produced_artifacts": [
                    {"artifact_type": "vendor_bill", "artifact_id": "artifact-1", "external_identity": "8100"}
                ],
            },
            started_at=datetime(2026, 9, 16, tzinfo=UTC),
            completed_at=datetime(2026, 9, 16, tzinfo=UTC),
        )
    )
    session.flush()


def test_no_execution_at_all_returns_false(session: Session) -> None:
    reader = SqlAlchemyVendorBillExecutionEvidenceReader(session)
    assert reader.has_successful_vendor_bill(review_id=REVIEW_ID, company_id=COMPANY_ID) is False


def test_completed_vendor_bill_step_returns_true(session: Session) -> None:
    _execution(session, execution_id="exec-1")
    _step(session, execution_id="exec-1")
    reader = SqlAlchemyVendorBillExecutionEvidenceReader(session)
    assert reader.has_successful_vendor_bill(review_id=REVIEW_ID, company_id=COMPANY_ID) is True


def test_pending_vendor_bill_step_returns_false(session: Session) -> None:
    _execution(session, execution_id="exec-2")
    _step(session, execution_id="exec-2", state="pending")
    reader = SqlAlchemyVendorBillExecutionEvidenceReader(session)
    assert reader.has_successful_vendor_bill(review_id=REVIEW_ID, company_id=COMPANY_ID) is False


def test_failed_vendor_bill_step_returns_false(session: Session) -> None:
    _execution(session, execution_id="exec-3")
    _step(session, execution_id="exec-3", state="failed")
    reader = SqlAlchemyVendorBillExecutionEvidenceReader(session)
    assert reader.has_successful_vendor_bill(review_id=REVIEW_ID, company_id=COMPANY_ID) is False


def test_completed_non_vendor_bill_step_returns_false(session: Session) -> None:
    _execution(session, execution_id="exec-4")
    _step(session, execution_id="exec-4", step_type="operating_expense")
    reader = SqlAlchemyVendorBillExecutionEvidenceReader(session)
    assert reader.has_successful_vendor_bill(review_id=REVIEW_ID, company_id=COMPANY_ID) is False


def test_completed_vendor_bill_for_different_review_does_not_leak(session: Session) -> None:
    _execution(session, execution_id="exec-5", review_id="review:other")
    _step(session, execution_id="exec-5")
    reader = SqlAlchemyVendorBillExecutionEvidenceReader(session)
    assert reader.has_successful_vendor_bill(review_id=REVIEW_ID, company_id=COMPANY_ID) is False


def test_completed_vendor_bill_for_different_company_does_not_leak(session: Session) -> None:
    session.add(
        WorkflowExecution(
            execution_id="exec-6",
            review_id=REVIEW_ID,
            decision_version=2,
            company_id=999,
            state="executing",
            mode="execute",
            idempotency_key="idem:exec-6",
            plan_signature="sig",
            plan={},
            checkpoint={},
            retry_policy={},
        )
    )
    session.flush()
    _step(session, execution_id="exec-6")
    reader = SqlAlchemyVendorBillExecutionEvidenceReader(session)
    assert reader.has_successful_vendor_bill(review_id=REVIEW_ID, company_id=COMPANY_ID) is False
