"""Explicit human-selected product resolution for one unmatched invoice line (P0-PROD-07D).

An operator's explicit ``LineResolution.selected_product_id`` is validated here --
read-only, before the decision is accepted -- against a minimal Odoo product
snapshot, then applied by substituting that one line's ``ProductMatchResult`` inside
the already-pinned ``InvoiceProductMatchResult`` with a normal ``MATCHED`` result
(``matched_by="human_selected"``).

This deliberately reuses the existing product-match evidence shape rather than
inventing a parallel one: once substituted, the line is indistinguishable to
``VendorBillBuilder``/``VendorBillExecutionStrategy`` from a deterministic match, so
neither needs to change, and execution never performs a live Odoo product lookup --
the validated selection is pinned once, here, at decision-acceptance time (mirroring
the read-before-decide shape of ``ValidateSupplierResolutionUseCase``).

Never inferred from ``PRODUCT_NOT_FOUND`` -- only applied for a line explicitly named
in ``LineResolution.selected_product_id``. The source invoice line (``seller_item_code``,
``buyer_item_code``, ``barcode``, ``description``, quantities) is never touched; only
the derived match result for that specific line is replaced.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from app.application.dto import ApplicationDTO
from app.application.workbench.dto import LineResolution
from app.application.workbench.exceptions import ReviewDecisionError
from app.matching import InvoiceProductLineResult, InvoiceProductMatchResult, ProductMatchStatus

HUMAN_SELECTED_MATCHED_BY = "human_selected"
HUMAN_SELECTED_REASON = "Explicit human-selected existing Odoo product."
HUMAN_SELECTED_CONFIDENCE = Decimal("1.00")


@dataclass(frozen=True, slots=True)
class ResolutionProductRecord(ApplicationDTO):
    """Minimal read-only projection of an Odoo ``product.product`` for selection validation."""

    id: int
    name: str | None
    default_code: str | None
    barcode: str | None
    active: bool
    company_id: int | None


def selected_product_ids(line_resolutions: tuple[LineResolution, ...]) -> tuple[int, ...]:
    """Distinct positive product ids explicitly selected across ``line_resolutions``."""

    ids: list[int] = []
    seen: set[int] = set()
    for resolution in line_resolutions:
        product_id = resolution.selected_product_id
        if product_id is not None and product_id not in seen:
            seen.add(product_id)
            ids.append(product_id)
    return tuple(ids)


def apply_selected_product_resolutions(
    product_match: InvoiceProductMatchResult,
    *,
    line_resolutions: tuple[LineResolution, ...],
    company_id: int,
    products_by_id: dict[int, ResolutionProductRecord],
) -> InvoiceProductMatchResult:
    """Validated substitution of pinned product-match evidence for explicitly selected lines.

    Fails closed (raises :class:`ReviewDecisionError`) the moment any explicitly
    selected product does not exist, is inactive, or is scoped to a different
    company -- the whole decision submission aborts rather than silently accepting
    one line's selection and dropping another's. A line with no explicit selection
    keeps its existing deterministic (or ``account_only``, handled separately) result
    completely unchanged.
    """

    selected_by_line = {
        resolution.line_number: resolution.selected_product_id
        for resolution in line_resolutions
        if resolution.selected_product_id is not None
    }
    if not selected_by_line:
        return product_match

    new_line_results = []
    for line_result in product_match.line_results:
        product_id = selected_by_line.get(line_result.line_number)
        if product_id is None:
            new_line_results.append(line_result)
            continue
        record = _validated_product_record(
            products_by_id.get(product_id),
            product_id=product_id,
            company_id=company_id,
        )
        new_line_results.append(
            InvoiceProductLineResult(
                line_number=line_result.line_number,
                # default_code/barcode/seller_item_code are the *source invoice line's*
                # identifiers, carried through unchanged -- see ProductMatchingEngine's
                # own _result() helper, which populates them the same way regardless of
                # match method. Only the match outcome itself changes here.
                result=replace(
                    line_result.result,
                    status=ProductMatchStatus.MATCHED,
                    product_id=record.id,
                    matched_by=HUMAN_SELECTED_MATCHED_BY,
                    reason=HUMAN_SELECTED_REASON,
                    candidate_count=1,
                    confidence=HUMAN_SELECTED_CONFIDENCE,
                ),
            )
        )
    return replace(product_match, line_results=tuple(new_line_results))


def _validated_product_record(
    record: ResolutionProductRecord | None,
    *,
    product_id: int,
    company_id: int,
) -> ResolutionProductRecord:
    if record is None:
        raise ReviewDecisionError("The selected product does not exist.")
    if type(record.id) is not int or record.id != product_id:
        raise ReviewDecisionError("The selected product id is invalid.")
    if not record.active:
        raise ReviewDecisionError("The selected product is archived/inactive.")
    if record.company_id not in (None, company_id):
        raise ReviewDecisionError("The selected product is scoped to a different company.")
    return record
