"""Read-only, artifact-derived Vendor Bill verification."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.application.dto import ApplicationDTO
from app.application.exceptions import ApplicationError
from app.application.execution.contracts import ExecutionArtifactType, ExecutionStepStatus, ExecutionStepType
from app.application.execution.ports import WorkbenchExecutionSnapshotReader
from app.application.execution.runtime import ExecutionState
from app.application.workbench.ports import ReviewQueueReader
from app.application.workbench.queries import ReviewDetailQuery


class VendorBillReadbackError(ApplicationError):
    error_category = "vendor_bill_readback_error"


class VendorBillReadbackUnavailableError(VendorBillReadbackError):
    """The persisted execution does not prove one successful Vendor Bill artifact."""

    error_category = "vendor_bill_readback_unavailable"


class VendorBillReadbackNotFoundError(VendorBillReadbackError):
    """The artifact-derived Vendor Bill does not exist in the scoped Odoo company."""

    error_category = "vendor_bill_readback_not_found"


class VendorBillReadbackIntegrityError(VendorBillReadbackError):
    """Persisted or Odoo verification data is malformed, ambiguous, or inconsistent."""

    error_category = "vendor_bill_readback_integrity_error"


@dataclass(frozen=True, slots=True)
class VendorBillHeaderVerification(ApplicationDTO):
    move_id: int
    company_id: int
    state: str
    move_type: str
    partner_id: int
    currency: str
    amount_untaxed: Decimal
    amount_tax: Decimal
    amount_total: Decimal


@dataclass(frozen=True, slots=True)
class VendorBillLineVerification(ApplicationDTO):
    line_id: int
    move_id: int
    account_id: int | None
    product_id: int | None
    quantity: Decimal
    price_unit: Decimal
    tax_ids: tuple[int, ...]
    price_subtotal: Decimal
    price_total: Decimal


@dataclass(frozen=True, slots=True)
class VendorBillReadback(ApplicationDTO):
    review_id: str
    execution_id: str
    artifact_id: str
    header: VendorBillHeaderVerification
    lines: tuple[VendorBillLineVerification, ...]


class VendorBillHeaderVerificationReader(Protocol):
    def read_vendor_bill(self, *, move_id: int, company_id: int) -> VendorBillHeaderVerification | None: ...


class VendorBillLineVerificationReader(Protocol):
    def read_invoice_lines_for_move(self, *, move_id: int) -> tuple[VendorBillLineVerification, ...]: ...


class GetVendorBillReadbackUseCase:
    """Resolve a move only from company-scoped persisted execution evidence, then read it."""

    def __init__(
        self,
        *,
        review_reader: ReviewQueueReader,
        execution_snapshot_reader: WorkbenchExecutionSnapshotReader,
        header_reader: VendorBillHeaderVerificationReader,
        line_reader: VendorBillLineVerificationReader,
    ) -> None:
        self._review_reader = review_reader
        self._execution_snapshot_reader = execution_snapshot_reader
        self._header_reader = header_reader
        self._line_reader = line_reader

    def execute(self, *, review_id: str, company_id: int) -> VendorBillReadback:
        self._review_reader.get_review_item(ReviewDetailQuery(review_id=review_id, company_id=company_id))
        snapshot = self._execution_snapshot_reader.find_latest_snapshot_for_review(
            review_id=review_id, company_id=company_id
        )
        if snapshot is None or snapshot.state is not ExecutionState.COMPLETED:
            raise VendorBillReadbackUnavailableError("A completed Vendor Bill execution is required.")

        artifacts = []
        for step in snapshot.steps:
            if (
                step.step_type is ExecutionStepType.VENDOR_BILL
                and step.last_result is not None
                and step.last_result.status is ExecutionStepStatus.EXECUTED
                and step.last_result.dry_run is False
            ):
                artifacts.extend(
                    artifact
                    for artifact in step.last_result.produced_artifacts
                    if artifact.artifact_type is ExecutionArtifactType.VENDOR_BILL
                )
        if len(artifacts) != 1:
            raise VendorBillReadbackUnavailableError("Exactly one successful Vendor Bill artifact is required.")

        artifact = artifacts[0]
        move_id = _artifact_move_id(artifact.artifact_id)
        header = self._header_reader.read_vendor_bill(move_id=move_id, company_id=company_id)
        if header is None:
            raise VendorBillReadbackNotFoundError("The persisted Vendor Bill artifact was not found in Odoo.")
        if header.move_id != move_id or header.company_id != company_id or header.move_type != "in_invoice":
            raise VendorBillReadbackIntegrityError("Odoo Vendor Bill verification returned inconsistent data.")
        lines = self._line_reader.read_invoice_lines_for_move(move_id=move_id)
        if not lines or any(line.move_id != move_id for line in lines):
            raise VendorBillReadbackIntegrityError("Odoo Vendor Bill line verification returned inconsistent data.")
        return VendorBillReadback(
            review_id=review_id,
            execution_id=snapshot.execution_id,
            artifact_id=artifact.artifact_id,
            header=header,
            lines=lines,
        )


def _artifact_move_id(artifact_id: str) -> int:
    if not isinstance(artifact_id, str) or not artifact_id.isascii() or not artifact_id.isdecimal():
        raise VendorBillReadbackIntegrityError("The Vendor Bill artifact id is invalid.")
    move_id = int(artifact_id)
    if move_id <= 0 or str(move_id) != artifact_id:
        raise VendorBillReadbackIntegrityError("The Vendor Bill artifact id is invalid.")
    return move_id
