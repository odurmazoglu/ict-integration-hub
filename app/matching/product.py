from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.domain.invoice import InternalInvoice, InvoiceLine
from app.domain.invoice.source_profiles import profile_manufacturer_sku
from app.erp.models import Product
from app.erp.provider import RepositoryProvider
from app.erp.repositories import SupplierProductRepository
from app.matching.exceptions import ProductMatchingError
from app.matching.result import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)

EXACT_MATCH_CONFIDENCE = Decimal("1.00")
MATCHED_BY_MANUFACTURER_ITEM_CODE = "manufacturer_item_code"
MATCHED_BY_SUPPLIER_PROFILE_SKU = "supplier_profile_sku"
MATCHED_BY_SUPPLIER_PRODUCT_CODE = "supplier_product_code"
# Two rows/variants are enough to prove ambiguity; reads never need to be larger.
SUPPLIER_PRODUCT_LOOKUP_LIMIT = 2

_Lookup = tuple[str, str | None, Callable[..., Sequence[Product]]]


class ProductMatchingEngine:
    """Deterministic product matcher.

    ``supplier_product_repository`` enables P0-PROD-19A-3 supplier-scoped matching through
    ``product.supplierinfo``; without it (or without a uniquely MATCHED ``partner_match``)
    supplierinfo never participates.
    """

    def __init__(
        self,
        provider: RepositoryProvider,
        *,
        supplier_product_repository: SupplierProductRepository | None = None,
    ) -> None:
        self._provider = provider
        self._supplier_product_repository = supplier_product_repository

    def match_invoice(
        self,
        invoice: object,
        *,
        company_id: int | None = None,
        partner_match: PartnerMatchResult | None = None,
    ) -> InvoiceProductMatchResult:
        if not isinstance(invoice, InternalInvoice):
            return InvoiceProductMatchResult(errors=("InternalInvoice DTO is required for product matching.",))
        if not invoice.lines:
            return InvoiceProductMatchResult(errors=("Invoice has no lines to match.",))

        line_results: list[InvoiceProductLineResult] = []
        supplier_partner_id = _resolved_supplier_partner_id(partner_match)
        for line in invoice.lines:
            result = self._match_line(invoice, line, company_id=company_id, supplier_partner_id=supplier_partner_id)
            line_results.append(InvoiceProductLineResult(line_number=line.line_number, result=result))
        return InvoiceProductMatchResult(line_results=tuple(line_results))

    def _match_line(
        self,
        invoice: InternalInvoice,
        line: InvoiceLine,
        *,
        company_id: int | None,
        supplier_partner_id: int | None,
    ) -> ProductMatchResult:
        if line.line_number is None or not line.line_number.strip():
            return _result(
                status=ProductMatchStatus.INVALID_INPUT,
                line=line,
                product_id=None,
                matched_by=None,
                reason="Line identifier is required for product matching.",
                candidate_count=0,
                confidence=None,
            )

        repository = self._provider.product_repository
        lookup_plan = (
            ("default_code", _clean(line.buyer_item_code), repository.find_by_default_code),
            ("barcode", _clean(line.barcode), repository.find_by_barcode),
            ("seller_item_code", _clean(line.seller_item_code), repository.find_by_default_code),
        )
        sku_plan = authoritative_sku_identities(invoice, line)
        if all(identifier is None for _, identifier, _ in lookup_plan) and not sku_plan:
            return _result(
                status=ProductMatchStatus.INVALID_INPUT,
                line=line,
                product_id=None,
                matched_by=None,
                reason="At least one deterministic product identifier is required.",
                candidate_count=0,
                confidence=None,
            )

        outcomes = [_legacy_chain_outcome(lookup_plan, company_id=company_id)]
        outcomes.extend(
            _lookup_outcome(matched_by, identifier, repository.find_by_default_code, company_id=company_id)
            for matched_by, identifier in sku_plan
        )
        supplier_outcome = self._supplier_product_outcome(
            _clean(line.seller_item_code),
            supplier_partner_id=supplier_partner_id,
            company_id=company_id,
        )
        if supplier_outcome is not None:
            outcomes.append(supplier_outcome)
        return _combined_result(line, tuple(outcomes))

    def _supplier_product_outcome(
        self,
        seller_item_code: str | None,
        *,
        supplier_partner_id: int | None,
        company_id: int | None,
    ) -> _IdentityOutcome | None:
        """``(resolved supplier, seller_item_code) -> product.supplierinfo -> one variant``.

        Participates only with a repository, a seller code, a uniquely resolved supplier
        partner and a company. Exactly one supplierinfo row is required; a row naming a
        variant identifies that variant; a template-level row identifies a variant only
        when the template has exactly one active variant in scope. Anything else is
        ambiguous (more than one) or absent (none) -- never a guessed variant.
        """

        repository = self._supplier_product_repository
        if (
            repository is None
            or seller_item_code is None
            or supplier_partner_id is None
            or type(company_id) is not int
            or company_id <= 0
        ):
            return None
        try:
            rows = repository.find_supplier_product_codes(
                partner_id=supplier_partner_id,
                product_code=seller_item_code,
                company_id=company_id,
                limit=SUPPLIER_PRODUCT_LOOKUP_LIMIT,
            )
            if len(rows) != 1:
                return _IdentityOutcome(matched_by=MATCHED_BY_SUPPLIER_PRODUCT_CODE, candidate_count=len(rows))
            variants = repository.find_template_variants(
                product_tmpl_id=rows[0].product_tmpl_id,
                company_id=company_id,
                variant_id=rows[0].product_id,
                limit=SUPPLIER_PRODUCT_LOOKUP_LIMIT,
            )
        except Exception as exc:
            raise ProductMatchingError("Supplier product repository lookup failed.") from exc
        active_variants = tuple(variant for variant in variants if variant.active)
        return _IdentityOutcome(
            matched_by=MATCHED_BY_SUPPLIER_PRODUCT_CODE,
            product_id=active_variants[0].id if len(active_variants) == 1 else None,
            candidate_count=len(active_variants),
        )


def _resolved_supplier_partner_id(partner_match: PartnerMatchResult | None) -> int | None:
    if not isinstance(partner_match, PartnerMatchResult) or partner_match.status is not PartnerMatchStatus.MATCHED:
        return None
    partner_id = partner_match.partner_id
    return partner_id if type(partner_id) is int and partner_id > 0 else None


@dataclass(frozen=True, slots=True)
class _IdentityOutcome:
    """One identity's deterministic lookup outcome (``matched_by`` is None when absent)."""

    matched_by: str | None
    product_id: int | None = None
    candidate_count: int = 0


def authoritative_sku_identities(invoice: InternalInvoice, line: InvoiceLine) -> tuple[tuple[str, str], ...]:
    """Authoritative manufacturer SKUs on ``line``, each matched against ERP ``default_code``.

    ``ManufacturersItemIdentification`` for every supplier; plus a supplier source-profile
    SKU (VİTEL whole-field Description) when ``profile_manufacturer_sku`` accepts one.
    """

    identities: list[tuple[str, str]] = []
    manufacturer_item_code = _clean(line.manufacturer_item_code)
    if manufacturer_item_code is not None:
        identities.append((MATCHED_BY_MANUFACTURER_ITEM_CODE, manufacturer_item_code))
    profile_sku = profile_manufacturer_sku(invoice, line)
    if profile_sku is not None:
        identities.append((MATCHED_BY_SUPPLIER_PROFILE_SKU, profile_sku))
    return tuple(identities)


def _legacy_chain_outcome(lookup_plan: tuple[_Lookup, ...], *, company_id: int | None) -> _IdentityOutcome:
    """The pre-19A-2 priority chain, unchanged: the first unique or ambiguous hit stops it."""

    for matched_by, identifier, lookup in lookup_plan:
        if identifier is None:
            continue
        outcome = _lookup_outcome(matched_by, identifier, lookup, company_id=company_id)
        if outcome.candidate_count > 0:
            return outcome
    return _IdentityOutcome(matched_by=None)


def _lookup_outcome(
    matched_by: str,
    identifier: str,
    lookup: Callable[..., Sequence[Product]],
    *,
    company_id: int | None,
) -> _IdentityOutcome:
    try:
        candidates = lookup(identifier, company_id=company_id)
    except Exception as exc:
        raise ProductMatchingError("Product repository lookup failed.") from exc
    active_candidates = _active_candidates(candidates)
    return _IdentityOutcome(
        matched_by=matched_by,
        product_id=active_candidates[0].id if len(active_candidates) == 1 else None,
        candidate_count=len(active_candidates),
    )


def _combined_result(line: InvoiceLine, outcomes: tuple[_IdentityOutcome, ...]) -> ProductMatchResult:
    """Fail closed on any ambiguity or any disagreement between identities.

    Ambiguity or a conflict is reported as ``MULTIPLE_MATCHES``, the existing persisted
    status, so evidence stays readable by earlier code (no new enum value).
    """

    ambiguous = next((outcome for outcome in outcomes if outcome.candidate_count > 1), None)
    if ambiguous is not None:
        return _result(
            status=ProductMatchStatus.MULTIPLE_MATCHES,
            line=line,
            product_id=None,
            matched_by=None,
            reason=f"Multiple active product candidates found by {ambiguous.matched_by}.",
            candidate_count=ambiguous.candidate_count,
            confidence=None,
        )

    matched = tuple(outcome for outcome in outcomes if outcome.product_id is not None)
    product_ids = tuple(dict.fromkeys(outcome.product_id for outcome in matched))
    if len(product_ids) > 1:
        conflicts = ", ".join(f"{outcome.matched_by} -> product {outcome.product_id}" for outcome in matched)
        return _result(
            status=ProductMatchStatus.MULTIPLE_MATCHES,
            line=line,
            product_id=None,
            matched_by=None,
            reason=f"Conflicting product identities resolve to different products: {conflicts}.",
            candidate_count=len(product_ids),
            confidence=None,
        )
    if product_ids:
        primary = matched[0].matched_by
        corroborating = tuple(outcome.matched_by for outcome in matched[1:])
        reason = f"Unique product match by {primary}."
        if corroborating:
            reason = f"Unique product match by {primary}; corroborated by {', '.join(map(str, corroborating))}."
        return _result(
            status=ProductMatchStatus.MATCHED,
            line=line,
            product_id=product_ids[0],
            matched_by=primary,
            reason=reason,
            candidate_count=1,
            confidence=EXACT_MATCH_CONFIDENCE,
        )

    return _result(
        status=ProductMatchStatus.NOT_FOUND,
        line=line,
        product_id=None,
        matched_by=None,
        reason="No active deterministic product candidate found.",
        candidate_count=0,
        confidence=None,
    )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _active_candidates(candidates: Sequence[Product]) -> tuple[Product, ...]:
    return tuple(candidate for candidate in candidates if candidate.active)


def _result(
    *,
    status: ProductMatchStatus,
    line: InvoiceLine,
    product_id: int | None,
    matched_by: str | None,
    reason: str,
    candidate_count: int,
    confidence: Decimal | None,
) -> ProductMatchResult:
    return ProductMatchResult(
        status=status,
        line_number=line.line_number,
        product_id=product_id if status == ProductMatchStatus.MATCHED else None,
        default_code=_clean(line.buyer_item_code),
        barcode=_clean(line.barcode),
        seller_item_code=_clean(line.seller_item_code),
        matched_by=matched_by,
        reason=reason,
        candidate_count=candidate_count,
        confidence=confidence,
    )
