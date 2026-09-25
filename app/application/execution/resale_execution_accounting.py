"""RESALE Vendor Bill execution account safety (P0-PROD-18F-2).

Runs inside Vendor Bill execution, before the first Odoo Vendor Bill write, and is the
only issuer of :class:`~app.billing.ValidatedResaleLineAccount`. For a decision accepted
under a RESALE purchase purpose it:

1. loads the decision's immutable RESALE accounting pin (P0-PROD-18F-1) and fails closed
   if it is missing, corrupt, for another review version, or does not describe exactly
   this decision's lines and products -- a historical RESALE decision without a pin can
   never execute and is never backfilled;
2. re-reads Odoo's *current* product/category accounting configuration through the
   P0-PROD-18D discovery use case, re-applies the P0-PROD-18E-1A eligibility policy with
   the current ``RESALE_PRODUCT_CATEGORY_IDS``, and fails closed on any
   accounting-relevant drift from the pin (display names are not accounting identity
   and are ignored);
3. proves no fiscal position that could reach the bill maps a pinned account
   (:mod:`app.application.execution.resale_fiscal_position`);
4. only then issues one validated account per line, whose ``account_id`` is the pin's.

It never updates the pin, never accepts Odoo's new account, never takes an account from
a request, and never writes to Odoo. A non-RESALE decision is untouched: it needs no pin,
reads nothing from Odoo here, and gets ``None``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol

from app.application.exceptions import ApplicationError
from app.application.execution.contracts import ExecutionSourceInvoice
from app.application.execution.exceptions import ExecutionSourceInvoiceError, ResaleExecutionAccountingError
from app.application.execution.resale_fiscal_position import FiscalPositionReader, check_fiscal_position_safety
from app.application.workbench.exceptions import PurchaseAccountProductNotFoundError
from app.application.workbench.purchase_account_discovery import (
    GetProductPurchaseAccountQuery,
    ProductPurchaseAccountResolution,
    PurchaseAccountSource,
)
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workbench.resale_accounting_pin import (
    ResaleAccountingLinePin,
    ResaleAccountingPin,
    ResaleAccountingSource,
)
from app.application.workbench.resale_decision_gate import (
    ProductPurchaseAccountResolver,
    PurchasePurposeHistoryReader,
)
from app.application.workbench.resale_product_eligibility import (
    ResaleProductEligibility,
    evaluate_resale_product_eligibility,
    normalize_resale_category_ids,
)
from app.billing import ValidatedResaleLineAccount, issue_validated_resale_line_account
from app.matching import ProductMatchStatus

#: The pinned account source and the P0-PROD-18D resolution source it must still match.
_EXPECTED_SOURCE = {ResaleAccountingSource.RESALE_PRODUCT_CATEGORY: PurchaseAccountSource.CATEGORY}


class ResaleAccountingPinReader(Protocol):
    """Hub-persistence-only read of an accepted decision's RESALE pin. No Odoo access."""

    def get_resale_accounting_pin(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
    ) -> ResaleAccountingPin | None:
        pass


def load_decision_resale_pin(
    *,
    pin_reader: ResaleAccountingPinReader,
    purpose_reader: PurchasePurposeHistoryReader,
    review_id: str,
    company_id: int,
    decision_version: int,
) -> ResaleAccountingPin | None:
    """The decision's own pin if it was accepted under RESALE, else ``None``; fail closed.

    RESALE-ness is the purchase purpose of the version the decision was accepted on
    (``decision_version - 1``) -- exactly what the decision gate evaluated.
    """

    accepted_on_version = decision_version - 1
    try:
        resolutions = purpose_reader.list_purchase_purpose_resolutions(review_id=review_id, company_id=company_id)
    except ApplicationError as exc:
        raise ResaleExecutionAccountingError("The purchase purpose could not be read safely.") from exc
    current = [resolution for resolution in resolutions if resolution.review_version == accepted_on_version]
    if len(current) > 1:
        raise ResaleExecutionAccountingError("More than one purchase purpose exists for the decision version.")
    is_resale = bool(current) and current[0].purchase_purpose is PurchasePurpose.RESALE
    try:
        pin = pin_reader.get_resale_accounting_pin(
            review_id=review_id, company_id=company_id, decision_version=decision_version
        )
    except ExecutionSourceInvoiceError as exc:
        raise ResaleExecutionAccountingError("The RESALE accounting pin is invalid.") from exc
    if not is_resale:
        if pin is not None:
            raise ResaleExecutionAccountingError(
                "A RESALE accounting pin exists for a decision not accepted under a RESALE purpose."
            )
        return None
    if pin is None:
        raise ResaleExecutionAccountingError(
            "This RESALE decision has no pinned accounting evidence (accepted before RESALE pinning); "
            "accept a new decision to execute it."
        )
    if pin.review_version != accepted_on_version:
        raise ResaleExecutionAccountingError("The RESALE accounting pin belongs to another review version.")
    return pin


class ResaleExecutionAccountingValidator:
    """Fail-closed pre-write validation for RESALE Vendor Bill execution."""

    def __init__(
        self,
        *,
        pin_reader: ResaleAccountingPinReader,
        purpose_reader: PurchasePurposeHistoryReader,
        product_account_resolver: ProductPurchaseAccountResolver,
        fiscal_position_reader: FiscalPositionReader,
        approved_category_ids: Iterable[int],
    ) -> None:
        self._pin_reader = pin_reader
        self._purpose_reader = purpose_reader
        self._product_account_resolver = product_account_resolver
        self._fiscal_position_reader = fiscal_position_reader
        self._approved_category_ids = normalize_resale_category_ids(approved_category_ids)

    def validate(self, source: ExecutionSourceInvoice) -> Mapping[str, ValidatedResaleLineAccount] | None:
        """``None`` for a non-RESALE decision; otherwise one validated account per line."""

        pin = load_decision_resale_pin(
            pin_reader=self._pin_reader,
            purpose_reader=self._purpose_reader,
            review_id=source.review_id,
            company_id=source.company_id,
            decision_version=source.decision_version,
        )
        if pin is None:
            return None
        pinned_lines = _pinned_lines_for_source(pin, source)
        self._require_no_drift(source.company_id, pinned_lines)
        self._require_fiscal_position_safety(source, pinned_lines)
        return {
            line_number: issue_validated_resale_line_account(
                line_number=line_number,
                product_id=pinned.product_id,
                account_id=pinned.pre_fiscal_position_account_id,
            )
            for line_number, pinned in pinned_lines.items()
        }

    def _require_no_drift(self, company_id: int, pinned_lines: dict[str, ResaleAccountingLinePin]) -> None:
        pins_by_product: dict[int, list[ResaleAccountingLinePin]] = {}
        for pinned in pinned_lines.values():
            pins_by_product.setdefault(pinned.product_id, []).append(pinned)
        for product_id, pins in pins_by_product.items():
            resolution = self._current_resolution(company_id, product_id)
            eligibility = evaluate_resale_product_eligibility(
                resolution, company_id=company_id, approved_category_ids=self._approved_category_ids
            )
            if not eligibility.eligible:
                blockers = ",".join(blocker.value for blocker in eligibility.blockers)
                raise ResaleExecutionAccountingError(
                    f"RESALE execution blocked: product {product_id} is no longer eligible for RESALE "
                    f"({blockers}); accept a new decision."
                )
            for pinned in pins:
                drifted = accounting_drift(pinned, resolution, eligibility)
                if drifted:
                    raise ResaleExecutionAccountingError(
                        f"RESALE execution blocked: Odoo accounting configuration for line {pinned.line_number} "
                        f"(product {product_id}) drifted from the accepted pin ({', '.join(drifted)}); "
                        "accept a new decision."
                    )

    def _current_resolution(self, company_id: int, product_id: int) -> ProductPurchaseAccountResolution:
        try:
            return self._product_account_resolver.execute(
                GetProductPurchaseAccountQuery(company_id=company_id, product_id=product_id)
            )
        except PurchaseAccountProductNotFoundError as exc:
            raise ResaleExecutionAccountingError(
                f"RESALE execution blocked: product {product_id} is no longer visible to this company."
            ) from exc
        except Exception as exc:  # any read failure (ERP transport/response included) fails closed
            raise ResaleExecutionAccountingError(
                "RESALE execution blocked: current Odoo accounting configuration could not be read safely."
            ) from exc

    def _require_fiscal_position_safety(
        self, source: ExecutionSourceInvoice, pinned_lines: dict[str, ResaleAccountingLinePin]
    ) -> None:
        partner_id = source.partner_match.partner_id
        if type(partner_id) is not int or partner_id <= 0:
            raise ResaleExecutionAccountingError("RESALE execution blocked: the supplier partner is not resolved.")
        try:
            check_fiscal_position_safety(
                self._fiscal_position_reader,
                company_id=source.company_id,
                partner_id=partner_id,
                pinned_account_ids=frozenset(pin.pre_fiscal_position_account_id for pin in pinned_lines.values()),
            )
        except ResaleExecutionAccountingError:
            raise
        except Exception as exc:  # any read failure (ERP transport/response included) fails closed
            raise ResaleExecutionAccountingError(
                "RESALE execution blocked: Odoo fiscal-position configuration could not be read safely."
            ) from exc


def accounting_drift(
    pinned: ResaleAccountingLinePin,
    resolution: ProductPurchaseAccountResolution,
    eligibility: ResaleProductEligibility,
) -> tuple[str, ...]:
    """Accounting-relevant fields where current Odoo configuration differs from the pin.

    Product active/company/storability, product-level override, category account
    status and the RESALE category allowlist are already enforced by ``eligibility``.
    Names (account and category) are descriptive, never identity, and are not compared.
    """

    account = eligibility.pre_fiscal_position_account
    drifted: list[str] = []
    if resolution.product_id != pinned.product_id or eligibility.product_id != pinned.product_id:
        drifted.append("product_id")
    if eligibility.category_id != pinned.product_categ_id:
        drifted.append("product_categ_id")
    if resolution.pre_fiscal_position_account_source is not _EXPECTED_SOURCE.get(pinned.account_source):
        drifted.append("account_source")
    if account is None:
        drifted.append("account")
        return tuple(drifted)
    if account.account_id != pinned.pre_fiscal_position_account_id:
        drifted.append("account_id")
    if account.code != pinned.pre_fiscal_position_account_code:
        drifted.append("account_code")
    if account.account_type != pinned.pre_fiscal_position_account_type:
        drifted.append("account_type")
    return tuple(drifted)


def _pinned_lines_for_source(
    pin: ResaleAccountingPin, source: ExecutionSourceInvoice
) -> dict[str, ResaleAccountingLinePin]:
    """The pin per invoice line, proven to describe exactly this decision's lines/products."""

    pinned_by_line = pin.by_line_number()
    source_line_numbers = [line.line_number for line in source.invoice.lines]
    if len(set(source_line_numbers)) != len(source_line_numbers) or set(source_line_numbers) != set(pinned_by_line):
        raise ResaleExecutionAccountingError("The RESALE accounting pin does not cover exactly every invoice line.")
    if any(resolution.account_only for resolution in source.line_resolutions):
        raise ResaleExecutionAccountingError("A RESALE decision cannot contain account-only lines.")
    results_by_line: dict[str | None, list] = {}
    for line_result in source.product_match.line_results:
        results_by_line.setdefault(line_result.line_number, []).append(line_result.result)
    for line_number, pinned in pinned_by_line.items():
        results = results_by_line.get(line_number, [])
        if (
            len(results) != 1
            or results[0].status is not ProductMatchStatus.MATCHED
            or results[0].product_id != pinned.product_id
        ):
            raise ResaleExecutionAccountingError(
                f"The RESALE accounting pin for line {line_number} does not match the decision's product."
            )
    return {line_number: pinned_by_line[line_number] for line_number in source_line_numbers}


__all__ = [
    "ResaleAccountingPinReader",
    "ResaleExecutionAccountingValidator",
    "accounting_drift",
    "load_decision_resale_pin",
]
