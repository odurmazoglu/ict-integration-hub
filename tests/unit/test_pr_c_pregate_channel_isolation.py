"""Pre-gate hardening N1: a failing child-line channel never blocks parent operator requests.

The operator request tick runs the Workbench child product line channel first and the
parent channel second, sharing one business and one ledger session. An unexpected
exception escaping one channel must be logged with its traceback, the shared sessions
reset, the other channel still processed, and the tick still reported as failed --
never silently swallowed. Retry, ledger and idempotency semantics stay per request.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from app.application.workbench.operator_request_ingestion import (
    OperatorActorDirectory,
    OperatorRequest,
    OperatorRequestAction,
    OperatorRequestIngestionResult,
    OperatorRequestIngestionWorkflow,
    OperatorRequestLedgerStatus,
    OperatorRequestOutcome,
)
from app.composition.operator_requests import (
    PARENT_CHANNEL,
    PRODUCT_LINE_CHANNEL,
    OperatorRequestChannel,
    OperatorRequestChannelError,
    SequentialOperatorRequestWorkflows,
)
from app.erp.exceptions import ErpRepositoryError
from app.models.workbench_operator_request import WorkbenchOperatorRequest
from app.persistence.workbench_operator_request_ledger import (
    OperatorRequestLedgerError,
    SqlAlchemyOperatorRequestLedger,
)
from app.workers.uyumsoft_inbound_poller import PeriodicTask, PeriodicTaskScheduler
from tests.unit.test_adr_0013_operator_request_ingestion import (
    OPERATOR,
    FakeAcknowledger,
    FakeIssuer,
    FakeLedger,
    FakeReader,
    FakeRefresher,
    ScriptedHandler,
    _done,
)

COMPANY = 1
REQUESTED_AT = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _loggers_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alembic's ``fileConfig`` (run by earlier migration tests) disables existing loggers;
    these tests assert on log records, so re-enable exactly the two loggers involved."""

    for name in ("app.composition.operator_requests", "app.workers.uyumsoft_inbound_poller"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)


def _parent_request(**overrides: Any) -> OperatorRequest:
    values: dict[str, Any] = {
        "odoo_record_id": 24,
        "review_id": "review:parent",
        "company_id": COMPANY,
        "action": OperatorRequestAction.PRODUCT_MAPPING,
        "expected_version": 3,
        "requested_by_odoo_user_id": 2,
        "requested_at": REQUESTED_AT,
        "line_number": "1",
        "product_id": 393,
    }
    values.update(overrides)
    return OperatorRequest(**values)


def _line_request() -> OperatorRequest:
    return _parent_request(
        odoo_record_id=21, review_id="review:child", action=OperatorRequestAction.PRODUCT_LINE_MAPPING, line_number="2"
    )


def _workflow(
    reader: Any,
    handler: ScriptedHandler,
    action: OperatorRequestAction,
    *,
    ledger: Any = None,
    acknowledger: FakeAcknowledger | None = None,
) -> OperatorRequestIngestionWorkflow:
    return OperatorRequestIngestionWorkflow(
        reader=reader,
        acknowledger=acknowledger or FakeAcknowledger(),
        ledger=ledger if ledger is not None else FakeLedger(),
        actors=OperatorActorDirectory({2: OPERATOR}),
        handlers={action: handler},
        authorization_issuer=FakeIssuer(),
        projection_refresher=FakeRefresher(),
        clock=lambda: datetime(2026, 10, 9, 10, 1, tzinfo=UTC),
        transient_errors=(ErpRepositoryError,),
    )


class Crashing:
    """A channel whose workflow escapes with an unexpected (non-classified) exception."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def run(self, *, company_id: int) -> OperatorRequestIngestionResult:
        self.calls += 1
        raise self.error


class Resets:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def __call__(self) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("rollback failed")


def _channels(child: Any, parent: Any) -> tuple[OperatorRequestChannel, OperatorRequestChannel]:
    return (
        OperatorRequestChannel(name=PRODUCT_LINE_CHANNEL, workflow=child),
        OperatorRequestChannel(name=PARENT_CHANNEL, workflow=parent),
    )


# --------------------------------------------------------------------------- isolation


def test_child_channel_crash_still_processes_parent_requests_and_reports_the_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    child = Crashing(RuntimeError("child channel exploded"))
    parent_handler = ScriptedHandler([_done("Ürün eşleştirildi.")])
    parent_ack = FakeAcknowledger()
    parent = _workflow(
        FakeReader([_parent_request()]),
        parent_handler,
        OperatorRequestAction.PRODUCT_MAPPING,
        acknowledger=parent_ack,
    )
    reset = Resets()

    with caplog.at_level(logging.ERROR), pytest.raises(OperatorRequestChannelError) as raised:
        SequentialOperatorRequestWorkflows(_channels(child, parent), reset=reset).run(company_id=COMPANY)

    # the parent request was fully processed and acknowledged despite the child crash
    assert len(parent_handler.calls) == 1
    assert [call["outcome"] for call in parent_ack.calls] == [OperatorRequestOutcome.COMPLETED]
    (result,) = raised.value.result.results
    assert result.outcome is OperatorRequestOutcome.COMPLETED and result.odoo_record_id == 24
    # not swallowed: logged with traceback, named, chained, and the tick still fails
    assert raised.value.failed_channels == (PRODUCT_LINE_CHANNEL,)
    assert isinstance(raised.value.__cause__, RuntimeError)
    failure_logs = [r for r in caplog.records if r.getMessage() == "workbench.operator_request.channel_failed"]
    assert len(failure_logs) == 1 and failure_logs[0].exc_info is not None
    assert failure_logs[0].channel == PRODUCT_LINE_CHANNEL
    # the shared sessions were reset exactly once, before the parent channel ran
    assert reset.calls == 1


def test_parent_channel_crash_keeps_child_results_and_reports_the_failure() -> None:
    child_handler = ScriptedHandler([_done("Ürün eşleştirildi.")])
    child = _workflow(FakeReader([_line_request()]), child_handler, OperatorRequestAction.PRODUCT_LINE_MAPPING)
    parent = Crashing(OperatorRequestLedgerError("Workbench operator request ledger could not be updated."))
    reset = Resets()

    with pytest.raises(OperatorRequestChannelError) as raised:
        SequentialOperatorRequestWorkflows(_channels(child, parent), reset=reset).run(company_id=COMPANY)

    assert raised.value.failed_channels == (PARENT_CHANNEL,)
    assert [r.outcome for r in raised.value.result.results] == [OperatorRequestOutcome.COMPLETED]
    assert reset.calls == 1


def test_healthy_channels_return_concatenated_results_without_reset() -> None:
    child = _workflow(
        FakeReader([_line_request()]), ScriptedHandler([_done()]), OperatorRequestAction.PRODUCT_LINE_MAPPING
    )
    parent = _workflow(
        FakeReader([_parent_request()]), ScriptedHandler([_done()]), OperatorRequestAction.PRODUCT_MAPPING
    )
    reset = Resets()

    result = SequentialOperatorRequestWorkflows(_channels(child, parent), reset=reset).run(company_id=COMPANY)

    assert [r.odoo_record_id for r in result.results] == [21, 24]
    assert reset.calls == 0


def test_both_channels_failing_names_both_and_chains_the_first() -> None:
    first, second = RuntimeError("first"), ValueError("second")
    reset = Resets()

    with pytest.raises(OperatorRequestChannelError) as raised:
        SequentialOperatorRequestWorkflows(_channels(Crashing(first), Crashing(second)), reset=reset).run(
            company_id=COMPANY
        )

    assert raised.value.failed_channels == (PRODUCT_LINE_CHANNEL, PARENT_CHANNEL)
    assert raised.value.__cause__ is first
    assert reset.calls == 2


def test_a_failed_reset_stops_before_any_further_channel(caplog: pytest.LogCaptureFixture) -> None:
    parent = Crashing(AssertionError("must not run"))

    with caplog.at_level(logging.ERROR), pytest.raises(OperatorRequestChannelError) as raised:
        SequentialOperatorRequestWorkflows(
            _channels(Crashing(RuntimeError("child")), parent), reset=Resets(fail=True)
        ).run(company_id=COMPANY)

    assert parent.calls == 0  # session state unproven -> fail closed, no further channel
    assert raised.value.failed_channels == (PRODUCT_LINE_CHANNEL,)
    assert any(r.getMessage() == "workbench.operator_request.channel_reset_failed" for r in caplog.records)


def test_classified_request_failures_stay_inside_the_channel_and_are_not_channel_crashes() -> None:
    """Handler exceptions are already per-request outcomes (FAILED/RETRY_LATER); no isolation path."""

    child = _workflow(
        FakeReader([_line_request()]),
        ScriptedHandler([RuntimeError("use case bug")]),
        OperatorRequestAction.PRODUCT_LINE_MAPPING,
    )
    parent = _workflow(
        FakeReader([_parent_request()]), ScriptedHandler([_done()]), OperatorRequestAction.PRODUCT_MAPPING
    )
    reset = Resets()

    result = SequentialOperatorRequestWorkflows(_channels(child, parent), reset=reset).run(company_id=COMPANY)

    assert [r.outcome for r in result.results] == [OperatorRequestOutcome.FAILED, OperatorRequestOutcome.COMPLETED]
    assert reset.calls == 0


def test_scheduler_records_the_tick_as_crashed_but_keeps_running(caplog: pytest.LogCaptureFixture) -> None:
    import threading

    parent_handler = ScriptedHandler([_done(), _done()])
    parent = _workflow(
        FakeReader([_parent_request()]), parent_handler, OperatorRequestAction.PRODUCT_MAPPING, ledger=FakeLedger()
    )
    composite = SequentialOperatorRequestWorkflows(_channels(Crashing(RuntimeError("child")), parent), reset=Resets())
    times = iter(range(0, 10_000, 100))
    scheduler = PeriodicTaskScheduler(
        tasks=[
            PeriodicTask(
                name="workbench_operator_requests", interval_seconds=60, run=lambda: composite.run(company_id=1)
            )
        ],
        stop_event=threading.Event(),
        monotonic=lambda: float(next(times)),
    )

    with caplog.at_level(logging.ERROR):
        assert scheduler.run(max_rounds=1) == 1

    assert len(parent_handler.calls) == 1
    assert any("periodic_task_crashed" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- realistic: real ledger + session


def _ledger_table_without_product_line_mapping(engine) -> None:
    """The ledger as it was at 202607170038: the child action violates the CHECK constraint."""

    ddl = str(CreateTable(WorkbenchOperatorRequest.__table__).compile(engine))
    assert ", 'product_line_mapping'" in ddl
    with engine.begin() as connection:
        connection.execute(text(ddl.replace(", 'product_line_mapping'", "")))


def test_unmigrated_ledger_breaks_only_the_child_channel_and_parent_requests_still_land() -> None:
    """The #210 failure mode, gate on without migration 0039: parent requests must not be blocked."""

    engine = create_engine("sqlite://")
    _ledger_table_without_product_line_mapping(engine)
    ledger_session = Session(engine)
    child_handler = ScriptedHandler([_done()])
    parent_handler = ScriptedHandler([_done("Ürün eşleştirildi.")])
    child_ack, parent_ack = FakeAcknowledger(), FakeAcknowledger()
    child = _workflow(
        FakeReader([_line_request()]),
        child_handler,
        OperatorRequestAction.PRODUCT_LINE_MAPPING,
        ledger=SqlAlchemyOperatorRequestLedger(ledger_session),
        acknowledger=child_ack,
    )
    parent = _workflow(
        FakeReader([_parent_request()]),
        parent_handler,
        OperatorRequestAction.PRODUCT_MAPPING,
        ledger=SqlAlchemyOperatorRequestLedger(ledger_session),
        acknowledger=parent_ack,
    )

    with pytest.raises(OperatorRequestChannelError) as raised:
        SequentialOperatorRequestWorkflows(_channels(child, parent), reset=ledger_session.rollback).run(
            company_id=COMPANY
        )

    assert raised.value.failed_channels == (PRODUCT_LINE_CHANNEL,)
    assert isinstance(raised.value.__cause__, OperatorRequestLedgerError)
    # child: nothing ran, nothing acknowledged -> the request stays ready for a later tick
    assert child_handler.calls == [] and child_ack.calls == []
    # parent: recorded, executed once, finished and acknowledged on the same ledger session
    assert len(parent_handler.calls) == 1
    assert [call["outcome"] for call in parent_ack.calls] == [OperatorRequestOutcome.COMPLETED]
    with Session(engine) as check:
        rows = check.scalars(select(WorkbenchOperatorRequest)).all()
    assert [(row.action, row.status) for row in rows] == [
        ("product_mapping", OperatorRequestLedgerStatus.COMPLETED.value)
    ]


def test_child_request_resumes_from_its_ledger_state_on_the_next_tick() -> None:
    """A crash after the ledger row was started keeps it in progress; the next tick resumes, never duplicates."""

    ledger = FakeLedger()
    handler = ScriptedHandler([_done("Ürün eşleştirildi.")])

    class CrashOnFirstAcknowledge(FakeAcknowledger):
        def __init__(self) -> None:
            super().__init__()
            self.crashed = False

        def acknowledge(self, **kwargs: Any) -> bool:
            if not self.crashed:
                self.crashed = True
                raise RuntimeError("unexpected acknowledgement bug")  # not an ApplicationError: escapes
            return super().acknowledge(**kwargs)

    crashing_ack = CrashOnFirstAcknowledge()
    child = _workflow(
        FakeReader([_line_request()]),
        handler,
        OperatorRequestAction.PRODUCT_LINE_MAPPING,
        ledger=ledger,
        acknowledger=crashing_ack,
    )
    parent = _workflow(FakeReader([]), ScriptedHandler([]), OperatorRequestAction.PRODUCT_MAPPING, ledger=ledger)
    composite = SequentialOperatorRequestWorkflows(_channels(child, parent), reset=Resets())

    with pytest.raises(OperatorRequestChannelError):
        composite.run(company_id=COMPANY)
    (entry,) = ledger.rows.values()
    assert entry.status is OperatorRequestLedgerStatus.COMPLETED  # Hub state committed before the ack crash

    result = composite.run(company_id=COMPANY)  # next tick

    assert len(handler.calls) == 1  # never re-executed
    assert [r.outcome for r in result.results] == [OperatorRequestOutcome.COMPLETED]
    assert [call["outcome"] for call in crashing_ack.calls] == [OperatorRequestOutcome.COMPLETED]


# --------------------------------------------------------------------------- composition wiring


def test_tick_wires_named_channels_child_first_with_a_session_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from app.composition import operator_requests
    from app.composition.operator_requests import _tick_workflow
    from app.core.config import Settings
    from tests.unit.test_odoo_supplier_partner_writer import FakeJson2Client
    from tests.unit.test_pr_c_product_line_requests import _mapping, _parent_mapping

    monkeypatch.setattr(operator_requests, "OdooWorkbenchFieldMapping", MagicMock())
    monkeypatch.setattr(operator_requests, "decision_mapping_for_requests", lambda base, request: MagicMock())
    business, ledger = MagicMock(spec=Session), MagicMock(spec=Session)

    composite = _tick_workflow(
        business_session=business,
        ledger_session=ledger,
        settings=Settings(),
        odoo_client=FakeJson2Client(),
        request_mapping=_parent_mapping(),
        line_request_mapping=_mapping(),
    )

    assert isinstance(composite, SequentialOperatorRequestWorkflows)
    assert [channel.name for channel in composite._channels] == [PRODUCT_LINE_CHANNEL, PARENT_CHANNEL]
    composite._reset()
    business.rollback.assert_called_once_with()
    ledger.rollback.assert_called_once_with()
