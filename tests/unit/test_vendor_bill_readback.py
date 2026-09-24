from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.application.execution import ExecutionArtifact, ExecutionArtifactType, ExecutionState, ExecutionStepStatus
from app.application.execution.contracts import ExecutionStepType
from app.application.workbench.vendor_bill_readback import (
    GetVendorBillReadbackUseCase,
    VendorBillHeaderVerification,
    VendorBillLineVerification,
    VendorBillReadbackIntegrityError,
    VendorBillReadbackNotFoundError,
    VendorBillReadbackUnavailableError,
)

REVIEW_ID = "review:b9aacadc-c67e-50b2-9183-bb730cb4709b"


class _ReviewReader:
    def __init__(self) -> None:
        self.queries = []

    def get_review_item(self, query):
        self.queries.append(query)
        return SimpleNamespace(review_id=query.review_id)


class _SnapshotReader:
    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot
        self.calls = []

    def find_latest_snapshot_for_review(self, *, review_id: str, company_id: int):
        self.calls.append((review_id, company_id))
        return self.snapshot


class _HeaderReader:
    def __init__(self, header) -> None:
        self.header = header
        self.calls = []

    def read_vendor_bill(self, *, move_id: int, company_id: int):
        self.calls.append((move_id, company_id))
        return self.header


class _LineReader:
    def __init__(self, lines) -> None:
        self.lines = lines
        self.calls = []

    def read_invoice_lines_for_move(self, *, move_id: int):
        self.calls.append(move_id)
        return self.lines


def _artifact(artifact_id: str = "63") -> ExecutionArtifact:
    return ExecutionArtifact(
        artifact_type=ExecutionArtifactType.VENDOR_BILL,
        artifact_id=artifact_id,
        external_identity="vendor-bill-write:test",
        created=True,
    )


def _snapshot(*artifacts: ExecutionArtifact, state=ExecutionState.COMPLETED):
    result = SimpleNamespace(status=ExecutionStepStatus.EXECUTED, dry_run=False, produced_artifacts=artifacts)
    step = SimpleNamespace(step_type=ExecutionStepType.VENDOR_BILL, last_result=result)
    return SimpleNamespace(execution_id="accepted-decision-execution:test", state=state, steps=(step,))


def _header(company_id: int = 7) -> VendorBillHeaderVerification:
    return VendorBillHeaderVerification(
        move_id=63,
        company_id=company_id,
        state="draft",
        move_type="in_invoice",
        partner_id=439,
        currency="TRY",
        amount_untaxed=Decimal("4959.80"),
        amount_tax=Decimal("991.96"),
        amount_total=Decimal("5951.76"),
    )


def _lines() -> tuple[VendorBillLineVerification, ...]:
    return (
        VendorBillLineVerification(
            line_id=101,
            move_id=63,
            account_id=247,
            product_id=None,
            quantity=Decimal("20"),
            price_unit=Decimal("74.397000"),
            tax_ids=(34,),
            price_subtotal=Decimal("1487.94"),
            price_total=Decimal("1785.53"),
        ),
    )


_DEFAULT = object()


def _use_case(snapshot, *, header=_DEFAULT, lines=_DEFAULT):
    if header is _DEFAULT:
        header = _header()
    if lines is _DEFAULT:
        lines = _lines()
    review_reader = _ReviewReader()
    snapshot_reader = _SnapshotReader(snapshot)
    header_reader = _HeaderReader(header)
    line_reader = _LineReader(lines)
    use_case = GetVendorBillReadbackUseCase(
        review_reader=review_reader,
        execution_snapshot_reader=snapshot_reader,
        header_reader=header_reader,
        line_reader=line_reader,
    )
    return use_case, review_reader, snapshot_reader, header_reader, line_reader


def test_success_derives_move_id_from_unique_successful_artifact_and_preserves_values() -> None:
    use_case, review_reader, snapshot_reader, header_reader, line_reader = _use_case(_snapshot(_artifact()))

    result = use_case.execute(review_id=REVIEW_ID, company_id=7)

    assert review_reader.queries[0].company_id == 7
    assert snapshot_reader.calls == [(REVIEW_ID, 7)]
    assert header_reader.calls == [(63, 7)]
    assert line_reader.calls == [63]
    assert result.header.partner_id == 439
    assert result.header.currency == "TRY"
    assert result.header.amount_total == Decimal("5951.76")
    assert result.lines[0].product_id is None
    assert result.lines[0].account_id == 247
    assert result.lines[0].tax_ids == (34,)
    assert result.lines[0].price_unit == Decimal("74.397000")


@pytest.mark.parametrize("artifact_id", ["0", "-1", "63.0", "063", "abc"])
def test_invalid_artifact_id_fails_before_odoo_read(artifact_id: str) -> None:
    use_case, _, _, header_reader, line_reader = _use_case(_snapshot(_artifact(artifact_id)))
    with pytest.raises(VendorBillReadbackIntegrityError):
        use_case.execute(review_id=REVIEW_ID, company_id=7)
    assert header_reader.calls == []
    assert line_reader.calls == []


@pytest.mark.parametrize("snapshot", [None, _snapshot(state=ExecutionState.FAILED), _snapshot()])
def test_missing_or_unsuccessful_artifact_fails_closed(snapshot) -> None:
    use_case, *_ = _use_case(snapshot)
    with pytest.raises(VendorBillReadbackUnavailableError):
        use_case.execute(review_id=REVIEW_ID, company_id=7)


def test_ambiguous_vendor_bill_artifact_fails_closed() -> None:
    use_case, _, _, header_reader, _ = _use_case(_snapshot(_artifact("63"), _artifact("64")))
    with pytest.raises(VendorBillReadbackUnavailableError):
        use_case.execute(review_id=REVIEW_ID, company_id=7)
    assert header_reader.calls == []


def test_odoo_bill_not_found_fails_closed() -> None:
    use_case, *_ = _use_case(_snapshot(_artifact()), header=None)
    with pytest.raises(VendorBillReadbackNotFoundError):
        use_case.execute(review_id=REVIEW_ID, company_id=7)


def test_company_mismatch_in_odoo_header_fails_closed() -> None:
    use_case, *_ = _use_case(_snapshot(_artifact()), header=_header(company_id=8))
    with pytest.raises(VendorBillReadbackIntegrityError):
        use_case.execute(review_id=REVIEW_ID, company_id=7)
