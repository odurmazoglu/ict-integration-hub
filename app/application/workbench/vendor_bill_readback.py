"""Read-only, artifact-derived Vendor Bill verification.

P0-PROD-18F-2: for a decision accepted under RESALE, the independently read Odoo lines are
also verified against the decision's immutable RESALE accounting pin (product and
account per line) and the bill's actual fiscal position is reported. A mismatch is
reported, never corrected -- readback never writes to Odoo.

P0-PROD-19E-1: every Vendor Bill is also verified monetarily -- currency, per-line
quantity, exact price_unit and subtotal, and the bill's untaxed/tax/total -- against the
accepted evidence at the bill currency's own precision
(:func:`app.billing.money.monetary_equal`). Same reporting model: a mismatch is reported
and the draft bill is left for reconciliation; nothing is corrected or re-created.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from app.application.dto import ApplicationDTO
from app.application.exceptions import ApplicationError
from app.application.execution.contracts import ExecutionArtifactType, ExecutionStepStatus, ExecutionStepType
from app.application.execution.exceptions import ResaleExecutionAccountingError
from app.application.execution.ports import WorkbenchExecutionSnapshotReader
from app.application.execution.resale_execution_accounting import ResaleAccountingPinReader, load_decision_resale_pin
from app.application.execution.runtime import ExecutionState
from app.application.workbench.ports import ReviewQueueReader
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.resale_accounting_pin import ResaleAccountingPin
from app.application.workbench.resale_decision_gate import PurchasePurposeHistoryReader
from app.billing.money import MonetaryPrecision, MonetaryPrecisionError, monetary_equal
from app.billing.reconciliation import monetary_mismatches

if TYPE_CHECKING:
    from app.application.execution.vendor_bill_preview import VendorBillPreview


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
    # P0-PROD-19E-1: the bill currency's res.currency.decimal_places; None when unread.
    currency_decimal_places: int | None = None


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


class ResaleReadbackStatus(StrEnum):
    VERIFIED = "verified"
    MISMATCH = "mismatch"


@dataclass(frozen=True, slots=True)
class ResaleReadbackLineCheck(ApplicationDTO):
    """One Odoo invoice line checked against the pinned RESALE product/account."""

    line_id: int
    product_id: int | None
    account_id: int | None
    expected_account_id: int | None
    product_matches: bool
    account_matches: bool


@dataclass(frozen=True, slots=True)
class VendorBillResaleReadbackVerification(ApplicationDTO):
    """Readback of a RESALE bill against its immutable pin (P0-PROD-18F-2).

    ``fiscal_position_id`` is what Odoo put on the bill (``None`` = none);
    ``fiscal_position_supported`` is ``False`` when this Odoo does not expose the field
    as expected, so ``None`` there means "not read", not "none".
    """

    status: ResaleReadbackStatus
    pin_review_version: int
    fiscal_position_supported: bool
    fiscal_position_id: int | None
    lines: tuple[ResaleReadbackLineCheck, ...]
    mismatches: tuple[str, ...] = field(default_factory=tuple)


class MonetaryReadbackStatus(StrEnum):
    VERIFIED = "verified"
    MISMATCH = "mismatch"


@dataclass(frozen=True, slots=True)
class MonetaryReadbackLineCheck(ApplicationDTO):
    """One Odoo invoice line matched to its expected line by (product, quantity, price_unit)."""

    line_id: int
    product_id: int | None
    quantity: Decimal
    price_unit: Decimal
    price_subtotal: Decimal
    expected_subtotal: Decimal | None
    matches: bool


@dataclass(frozen=True, slots=True)
class VendorBillMonetaryReadbackVerification(ApplicationDTO):
    """A created bill's money checked against the accepted evidence (P0-PROD-19E-1)."""

    status: MonetaryReadbackStatus
    currency: str | None
    currency_decimal_places: int | None
    expected_untaxed: Decimal | None
    expected_tax: Decimal | None
    expected_total: Decimal | None
    lines: tuple[MonetaryReadbackLineCheck, ...] = field(default_factory=tuple)
    mismatches: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class VendorBillReadback(ApplicationDTO):
    review_id: str
    execution_id: str
    artifact_id: str
    header: VendorBillHeaderVerification
    lines: tuple[VendorBillLineVerification, ...]
    # P0-PROD-18F-2: set only for a decision accepted under RESALE.
    resale_verification: VendorBillResaleReadbackVerification | None = None
    # P0-PROD-19E-1: set whenever a monetary verifier is composed.
    monetary_verification: VendorBillMonetaryReadbackVerification | None = None


class VendorBillExpectationReader(Protocol):
    """The Vendor Bill EXECUTE builds for an accepted decision, from its pinned evidence.

    Satisfied by the zero-write Vendor Bill preview, which shares the builder and pinned
    evidence with real execution -- readback never re-implements that arithmetic.
    """

    def expected_vendor_bill(self, *, review_id: str, company_id: int, decision_version: int) -> VendorBillPreview:
        pass


class VendorBillMonetaryReadbackVerifier:
    """Verify a created bill's currency, lines and totals at the currency's precision. Read-only."""

    def __init__(self, *, expectation_reader: VendorBillExpectationReader) -> None:
        self._expectation_reader = expectation_reader

    def verify(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
        header: VendorBillHeaderVerification,
        lines: tuple[VendorBillLineVerification, ...],
    ) -> VendorBillMonetaryReadbackVerification:
        try:
            expected = self._expectation_reader.expected_vendor_bill(
                review_id=review_id, company_id=company_id, decision_version=decision_version
            )
        except ApplicationError as exc:
            return _monetary_mismatch(header, (f"expected Vendor Bill unavailable: {exc.safe_message}",))
        try:
            precision = MonetaryPrecision(header.currency_decimal_places)  # type: ignore[arg-type]
        except MonetaryPrecisionError:
            return _monetary_mismatch(header, ("bill currency precision unavailable",))
        return compare_vendor_bill_money(expected, header, lines, precision)


def compare_vendor_bill_money(
    expected: VendorBillPreview,
    header: VendorBillHeaderVerification,
    lines: tuple[VendorBillLineVerification, ...],
    precision: MonetaryPrecision,
) -> VendorBillMonetaryReadbackVerification:
    """Pure comparison of an Odoo bill against the expected bill and source totals."""

    mismatches: list[str] = []
    if header.currency.strip().upper() != expected.currency_code.strip().upper():
        mismatches.append(f"currency {header.currency} != expected {expected.currency_code}")
    if len(lines) != len(expected.lines):
        mismatches.append(f"line count {len(lines)} != expected {len(expected.lines)}")

    # Odoo lines carry no source line number: pair each with an unused expected line of the
    # same product, quantity and exact price_unit (the posting price is never rounded).
    unused = list(expected.lines)
    checks: list[MonetaryReadbackLineCheck] = []
    for line in lines:
        partner = next(
            (
                candidate
                for candidate in unused
                if candidate.product_id == line.product_id
                and candidate.quantity == line.quantity
                and candidate.unit_price == line.price_unit
            ),
            None,
        )
        expected_subtotal = partner.currency_subtotal if partner is not None else None
        matches = expected_subtotal is not None and monetary_equal(line.price_subtotal, expected_subtotal, precision)
        if partner is None:
            mismatches.append(
                f"line {line.line_id}: no expected line with product {line.product_id}, "
                f"quantity {line.quantity}, price_unit {line.price_unit}"
            )
        else:
            unused.remove(partner)
            if not matches:
                mismatches.append(
                    f"line {line.line_id}: subtotal {line.price_subtotal} != expected {expected_subtotal}"
                )
        checks.append(
            MonetaryReadbackLineCheck(
                line_id=line.line_id,
                product_id=line.product_id,
                quantity=line.quantity,
                price_unit=line.price_unit,
                price_subtotal=line.price_subtotal,
                expected_subtotal=expected_subtotal,
                matches=matches,
            )
        )
    mismatches.extend(
        monetary_mismatches(
            (
                ("untaxed vs expected lines", header.amount_untaxed, expected.preview_untaxed),
                ("untaxed vs source", header.amount_untaxed, expected.source_untaxed),
                ("tax vs source", header.amount_tax, expected.source_tax),
                ("total vs source", header.amount_total, expected.source_total),
                ("total vs untaxed + tax", header.amount_total, header.amount_untaxed + header.amount_tax),
            ),
            precision,
        )
    )
    return VendorBillMonetaryReadbackVerification(
        status=MonetaryReadbackStatus.MISMATCH if mismatches else MonetaryReadbackStatus.VERIFIED,
        currency=header.currency,
        currency_decimal_places=precision.decimal_places,
        expected_untaxed=expected.source_untaxed,
        expected_tax=expected.source_tax,
        expected_total=expected.source_total,
        lines=tuple(checks),
        mismatches=tuple(mismatches),
    )


def _monetary_mismatch(
    header: VendorBillHeaderVerification, mismatches: tuple[str, ...]
) -> VendorBillMonetaryReadbackVerification:
    return VendorBillMonetaryReadbackVerification(
        status=MonetaryReadbackStatus.MISMATCH,
        currency=header.currency,
        currency_decimal_places=header.currency_decimal_places,
        expected_untaxed=None,
        expected_tax=None,
        expected_total=None,
        mismatches=mismatches,
    )


class VendorBillFiscalPositionReader(Protocol):
    def vendor_bill_fiscal_position_supported(self) -> bool: ...

    def read_vendor_bill_fiscal_position_id(self, *, move_id: int, company_id: int) -> int | None: ...


class VendorBillResaleReadbackVerifier:
    """Verify a created RESALE bill's lines against the decision's immutable pin. Read-only."""

    def __init__(
        self,
        *,
        pin_reader: ResaleAccountingPinReader,
        purpose_reader: PurchasePurposeHistoryReader,
        fiscal_position_reader: VendorBillFiscalPositionReader,
    ) -> None:
        self._pin_reader = pin_reader
        self._purpose_reader = purpose_reader
        self._fiscal_position_reader = fiscal_position_reader

    def verify(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
        move_id: int,
        lines: tuple[VendorBillLineVerification, ...],
    ) -> VendorBillResaleReadbackVerification | None:
        try:
            pin = load_decision_resale_pin(
                pin_reader=self._pin_reader,
                purpose_reader=self._purpose_reader,
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
            )
        except ResaleExecutionAccountingError as exc:
            raise VendorBillReadbackIntegrityError(exc.safe_message) from exc
        if pin is None:
            return None
        supported = self._fiscal_position_reader.vendor_bill_fiscal_position_supported()
        fiscal_position_id = (
            self._fiscal_position_reader.read_vendor_bill_fiscal_position_id(move_id=move_id, company_id=company_id)
            if supported
            else None
        )
        checks, mismatches = compare_resale_lines(pin, lines)
        return VendorBillResaleReadbackVerification(
            status=ResaleReadbackStatus.MISMATCH if mismatches else ResaleReadbackStatus.VERIFIED,
            pin_review_version=pin.review_version,
            fiscal_position_supported=supported,
            fiscal_position_id=fiscal_position_id,
            lines=checks,
            mismatches=mismatches,
        )


def compare_resale_lines(
    pin: ResaleAccountingPin, lines: tuple[VendorBillLineVerification, ...]
) -> tuple[tuple[ResaleReadbackLineCheck, ...], tuple[str, ...]]:
    """Per-line product/account checks plus whole-bill product multiset/line-count checks.

    Odoo invoice lines carry no source line number, so lines are matched by product: the
    pin gives exactly one account per product (it is derived from the product's
    category), and the bill must hold exactly the pinned products, each on its pinned
    account.
    """

    account_by_product: dict[int, int] = {}
    for pinned in pin.lines:
        if account_by_product.setdefault(pinned.product_id, pinned.pre_fiscal_position_account_id) != (
            pinned.pre_fiscal_position_account_id
        ):
            raise VendorBillReadbackIntegrityError("The RESALE accounting pin names two accounts for one product.")
    mismatches: list[str] = []
    if len(lines) != len(pin.lines):
        mismatches.append(f"line count {len(lines)} != pinned {len(pin.lines)}")
    if Counter(line.product_id for line in lines) != Counter(pinned.product_id for pinned in pin.lines):
        mismatches.append("products differ from the pin")
    checks: list[ResaleReadbackLineCheck] = []
    for line in lines:
        expected_account_id = account_by_product.get(line.product_id) if line.product_id is not None else None
        product_matches = expected_account_id is not None
        account_matches = product_matches and line.account_id == expected_account_id
        if not product_matches:
            mismatches.append(f"line {line.line_id}: product {line.product_id} is not pinned")
        elif not account_matches:
            mismatches.append(f"line {line.line_id}: account {line.account_id} != pinned {expected_account_id}")
        checks.append(
            ResaleReadbackLineCheck(
                line_id=line.line_id,
                product_id=line.product_id,
                account_id=line.account_id,
                expected_account_id=expected_account_id,
                product_matches=product_matches,
                account_matches=account_matches,
            )
        )
    return tuple(checks), tuple(mismatches)


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
        resale_verifier: VendorBillResaleReadbackVerifier | None = None,
        monetary_verifier: VendorBillMonetaryReadbackVerifier | None = None,
    ) -> None:
        self._review_reader = review_reader
        self._execution_snapshot_reader = execution_snapshot_reader
        self._header_reader = header_reader
        self._line_reader = line_reader
        self._resale_verifier = resale_verifier
        self._monetary_verifier = monetary_verifier

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
        resale_verification = (
            self._resale_verifier.verify(
                review_id=review_id,
                company_id=company_id,
                decision_version=snapshot.decision_version,
                move_id=move_id,
                lines=lines,
            )
            if self._resale_verifier is not None
            else None
        )
        monetary_verification = (
            self._monetary_verifier.verify(
                review_id=review_id,
                company_id=company_id,
                decision_version=snapshot.decision_version,
                header=header,
                lines=lines,
            )
            if self._monetary_verifier is not None
            else None
        )
        return VendorBillReadback(
            review_id=review_id,
            execution_id=snapshot.execution_id,
            artifact_id=artifact.artifact_id,
            header=header,
            lines=lines,
            resale_verification=resale_verification,
            monetary_verification=monetary_verification,
        )


def _artifact_move_id(artifact_id: str) -> int:
    if not isinstance(artifact_id, str) or not artifact_id.isascii() or not artifact_id.isdecimal():
        raise VendorBillReadbackIntegrityError("The Vendor Bill artifact id is invalid.")
    move_id = int(artifact_id)
    if move_id <= 0 or str(move_id) != artifact_id:
        raise VendorBillReadbackIntegrityError("The Vendor Bill artifact id is invalid.")
    return move_id
