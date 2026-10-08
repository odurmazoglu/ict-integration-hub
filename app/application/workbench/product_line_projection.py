"""PR B: read-only per-invoice-line product projection for the Odoo Workbench.

One projected line is one immutable source invoice line of a review. The line rows
are a *projection* of committed Hub truth -- never a second workflow source of truth,
never an operator input -- and are written to the Studio child model
``x_ipp_wb_product_line`` only by the canonical projection sync.

Everything here is pure: the composition root reads the committed facts (source
invoice, the classification version's Stage-1 execution evidence, the accepted
decision's effective resolution, the review's reclassification history) and this
module derives the projected lines from them.

Committed-truth boundary: projection never matches anything. There is no Odoo call,
no supplier re-resolution, no approximate or name-based inference and no product lookup in
current master data. The supplier is the one the *persisted* product matching ran
under; the product is the one *persisted* evidence or the accepted decision names. A
fact the Hub has not committed is projected as empty, never guessed.

Evidence -> state mapping (the only states this module ever produces):

Pending review (``CURRENT_BLOCKERS``), with Stage-1 execution evidence for the
current version -- that evidence exists only when the effective supplier matched:

* line result ``MATCHED`` with a product id      -> ``matched`` (product, matched_by)
* line result ``NOT_FOUND``                      -> ``product_not_found``
* line result ``MULTIPLE_MATCHES``               -> ``product_ambiguous``
* line result ``INVALID_INPUT``                  -> ``identifier_missing``
* a current line-scoped product reason always wins over a ``MATCHED`` result
  (fail closed: a line the review still blocks on is never shown as matched).

Pending review without such evidence (supplier not matched, or the Stage-1 gate did
not pin evidence), from the current line-scoped review reasons only:

* ``PRODUCT_IDENTIFIER_MISSING``                 -> ``identifier_missing``
* ``PRODUCT_AMBIGUOUS``                          -> ``product_ambiguous``
* ``PRODUCT_NOT_FOUND`` + a supplier blocker     -> ``supplier_unresolved``
* ``PRODUCT_NOT_FOUND`` otherwise                -> ``product_not_found``
* no product reason for the line                 -> ``no_evidence`` (never "matched":
  without evidence there is no product id to show)

Decided review (``DECISION_BASIS``), from the accepted decision's effective resolution:

* kind ``product`` with a product id             -> ``matched`` (matched_by from the
  accepted evidence, else the product source)
* ``account_only`` / ``accounting_resolution`` /
  ``operating_expense_mapping`` / ``fixed_asset``  -> ``resolved_without_product``
* ``unresolved``, or no effective resolution       -> ``no_evidence``
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.application.dto import ApplicationDTO
from app.application.workbench.dto import ReviewReasonsRole
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode
from app.matching import PartnerMatchStatus, ProductMatchStatus

#: Versioned so a future change of the identity scheme can never collide with v1 rows.
PRODUCT_LINE_KEY_PREFIX = "ipp-pl:v1"

#: Line-scoped reasons the deterministic product matcher emits (``rules.deterministic``).
PRODUCT_LINE_REASON_CODES = frozenset(
    {
        ManualReviewReasonCode.PRODUCT_NOT_FOUND,
        ManualReviewReasonCode.PRODUCT_AMBIGUOUS,
        ManualReviewReasonCode.PRODUCT_IDENTIFIER_MISSING,
    }
)
#: Any product-matching reason marks the review as a product-resolution review.
PRODUCT_REVIEW_REASON_CODES: frozenset[str] = frozenset(
    PRODUCT_LINE_REASON_CODES | {ManualReviewReasonCode.PRODUCT_MAPPING_INCOMPLETE}
)
SUPPLIER_BLOCKER_CODES = frozenset(
    {
        ManualReviewReasonCode.SUPPLIER_NOT_FOUND,
        ManualReviewReasonCode.SUPPLIER_AMBIGUOUS,
        ManualReviewReasonCode.SUPPLIER_TAX_NUMBER_MISSING,
    }
)
_PRODUCT_RESOLUTION_KIND = "product"
#: ``EffectiveLineResolutionKind`` values that resolve a line without any product.
#: ``unresolved`` (and any unknown kind) is deliberately absent: it maps to ``no_evidence``.
_ACCOUNT_RESOLUTION_KINDS = frozenset(
    {"account_only", "accounting_resolution", "operating_expense_mapping", "fixed_asset"}
)


class ProductLineMatchState(StrEnum):
    """Projected line state; the value is the Studio selection key."""

    MATCHED = "matched"
    PRODUCT_NOT_FOUND = "product_not_found"
    PRODUCT_AMBIGUOUS = "product_ambiguous"
    IDENTIFIER_MISSING = "identifier_missing"
    SUPPLIER_UNRESOLVED = "supplier_unresolved"
    RESOLVED_WITHOUT_PRODUCT = "resolved_without_product"
    NO_EVIDENCE = "no_evidence"


#: Studio selection labels (``x_studio_ipp_match_state``); keys are the enum values.
PRODUCT_LINE_STATE_LABELS: dict[ProductLineMatchState, str] = {
    ProductLineMatchState.MATCHED: "Eşleşti",
    ProductLineMatchState.PRODUCT_NOT_FOUND: "Ürün Bulunamadı",
    ProductLineMatchState.PRODUCT_AMBIGUOUS: "Birden Fazla Aday",
    ProductLineMatchState.IDENTIFIER_MISSING: "Ürün Tanımlayıcısı Yok",
    ProductLineMatchState.SUPPLIER_UNRESOLVED: "Tedarikçi Kesin Değil",
    ProductLineMatchState.RESOLVED_WITHOUT_PRODUCT: "Ürünsüz Çözüldü",
    ProductLineMatchState.NO_EVIDENCE: "Kanıt Yok",
}

_EVIDENCE_STATES: dict[ProductMatchStatus, ProductLineMatchState] = {
    ProductMatchStatus.NOT_FOUND: ProductLineMatchState.PRODUCT_NOT_FOUND,
    ProductMatchStatus.MULTIPLE_MATCHES: ProductLineMatchState.PRODUCT_AMBIGUOUS,
    ProductMatchStatus.INVALID_INPUT: ProductLineMatchState.IDENTIFIER_MISSING,
}
_REASON_STATES: dict[ManualReviewReasonCode, ProductLineMatchState] = {
    ManualReviewReasonCode.PRODUCT_NOT_FOUND: ProductLineMatchState.PRODUCT_NOT_FOUND,
    ManualReviewReasonCode.PRODUCT_AMBIGUOUS: ProductLineMatchState.PRODUCT_AMBIGUOUS,
    ManualReviewReasonCode.PRODUCT_IDENTIFIER_MISSING: ProductLineMatchState.IDENTIFIER_MISSING,
}


@dataclass(frozen=True, slots=True)
class WorkbenchProductLineProjection(ApplicationDTO):
    """One projected source invoice line (all values Hub-owned, read-only in Odoo)."""

    line_key: str
    review_id: str
    company_id: int
    review_version: int
    line_number: str
    #: UI ordering only (never identity): see :func:`line_sequences`.
    line_sequence: int
    supplier_partner_id: int | None
    seller_item_code: str | None
    description: str | None
    quantity: Decimal | None
    unit_code: str | None
    match_state: ProductLineMatchState
    product_id: int | None
    matched_by: str | None
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.match_state, ProductLineMatchState):
            raise WorkbenchContractError("match_state must be a canonical ProductLineMatchState.")
        if (self.product_id is not None) != (self.match_state is ProductLineMatchState.MATCHED):
            raise WorkbenchContractError("Only a matched line carries a product, and it always does.")


class SourceLine(Protocol):
    @property
    def line_number(self) -> str | None: ...
    @property
    def seller_item_code(self) -> str | None: ...
    @property
    def description(self) -> str | None: ...
    @property
    def quantity(self) -> Decimal | None: ...
    @property
    def unit_code(self) -> str | None: ...


class LineMatch(Protocol):
    """A Stage-1 ``ProductMatchResult``."""

    @property
    def status(self) -> ProductMatchStatus: ...
    @property
    def product_id(self) -> int | None: ...
    @property
    def matched_by(self) -> str | None: ...


class DecisionLineResolution(Protocol):
    """The accepted decision's effective resolution of one line (``EffectiveLineResolution`` shape)."""

    @property
    def kind(self) -> str: ...
    @property
    def product_id(self) -> int | None: ...
    @property
    def product_source(self) -> str | None: ...
    @property
    def matched_by(self) -> str | None: ...


class SupplierMatch(Protocol):
    @property
    def status(self) -> PartnerMatchStatus: ...
    @property
    def partner_id(self) -> int | None: ...


@dataclass(frozen=True, slots=True)
class ProductLineFacts:
    """Committed facts for one review, read by the composition root in one read scope.

    ``evidence_lines`` is ``None`` when the classification version has no Stage-1
    execution evidence; ``decision_resolutions`` is ``None`` unless an accepted
    decision governs the review (``DECISION_BASIS`` with pinned execution evidence).
    """

    review_id: str
    company_id: int
    review_version: int
    invoice_number: str | None
    reasons: tuple[ManualReviewReason, ...]
    reasons_role: ReviewReasonsRole
    source_lines: Sequence[SourceLine]
    supplier_match: SupplierMatch | None = None
    evidence_lines: Mapping[str, LineMatch] | None = None
    decision_resolutions: Mapping[str, DecisionLineResolution] | None = None
    #: Committed reclassification history named a product reason at some earlier version.
    had_product_reasons: bool = False


@dataclass(frozen=True, slots=True)
class ProductLineReadFacts:
    """What the composition root reads for one review's product lines (one read scope).

    ``evidence_lines``/``supplier_match`` come from the Stage-1 execution evidence of
    the requested version and are ``None`` when that version has none (or none was
    requested -- a decided review uses its accepted decision evidence instead).
    """

    source_lines: Sequence[SourceLine]
    supplier_match: SupplierMatch | None = None
    evidence_lines: Mapping[str, LineMatch] | None = None
    had_product_reasons: bool = False


def line_sequences(line_numbers: Sequence[str]) -> tuple[int, ...]:
    """Deterministic numeric UI order for already-validated (non-blank, unique) line ids.

    * Every id is a plain decimal integer and their integer values are distinct ->
      the integer itself (``1, 2, 10`` sort as 1, 2, 10; ``000001`` -> 1).
    * Otherwise (any non-numeric id, or ``1``/``01`` colliding) -> the 1-based ordinal
      position in the immutable source invoice, for *every* line of the review, so a
      mix of numeric and non-numeric ids can never produce equal sequences.

    Only the immutable source line ids and their source order are used -- never a
    description, seller code or any mutable field. The line key is unaffected.
    """

    if all(_is_plain_integer(number) for number in line_numbers):
        values = tuple(int(number) for number in line_numbers)
        if len(set(values)) == len(values):
            return values
    return tuple(range(1, len(line_numbers) + 1))


def _is_plain_integer(value: str) -> bool:
    return value.isascii() and value.isdigit() and len(value) <= 9


def product_line_key(*, company_id: int, review_id: str, line_number: str) -> str:
    """Deterministic Hub-owned identity of one source invoice line's child row.

    ``review_id`` is itself derived from the immutable source invoice identity and is
    stable across review versions; the UBL line number (``cbc:ID``) is the only line
    identity the source model has, and it is only guaranteed unique *within* one
    invoice -- so the key always binds it to company + review. Version, description
    and seller code are deliberately excluded: a reclassification updates the same row.
    """

    line = (line_number or "").strip()
    if type(company_id) is not int or company_id <= 0:
        raise WorkbenchContractError("company_id must be a positive id.")
    if not isinstance(review_id, str) or not review_id.strip():
        raise WorkbenchContractError("review_id is required.")
    if not line:
        raise WorkbenchContractError("A source invoice line without a line number has no stable identity.")
    return f"{PRODUCT_LINE_KEY_PREFIX}:{company_id}:{review_id.strip()}:{line}"


def build_product_line_projections(facts: ProductLineFacts) -> tuple[WorkbenchProductLineProjection, ...]:
    """Every source line of a product-resolution review, or ``()`` when the review is not one.

    Raises :class:`WorkbenchContractError` when the source lines have no unique
    identity (blank or repeated line numbers): such a review gets no line rows rather
    than rows that could be confused with each other.
    """

    if not _is_product_review(facts):
        return ()
    numbers = [(line.line_number or "").strip() for line in facts.source_lines]
    if any(not number for number in numbers) or len(set(numbers)) != len(numbers):
        raise WorkbenchContractError(
            "Source invoice line numbers are blank or repeated; product lines are not projected for this review."
        )
    supplier = _supplier_partner_id(facts.supplier_match)
    line_reasons = _line_reasons(facts.reasons)
    supplier_blocked = any(reason.code in SUPPLIER_BLOCKER_CODES for reason in facts.reasons)
    return tuple(
        _line_projection(
            facts,
            line,
            number=number,
            sequence=sequence,
            supplier=supplier,
            reason=line_reasons.get(number),
            supplier_blocked=supplier_blocked,
        )
        for line, number, sequence in zip(facts.source_lines, numbers, line_sequences(numbers), strict=True)
    )


def _is_product_review(facts: ProductLineFacts) -> bool:
    """Exact line inclusion rule (see module docstring for the evidence semantics).

    A review is a product-resolution review when any of these committed facts holds:

    1. its reasons (current blockers or decision basis) contain a product reason;
    2. its committed reclassification history contains a product reason -- sticky, so
       a review whose last PRODUCT_NOT_FOUND line was just mapped keeps its rows;
    3. pending: its current Stage-1 evidence has at least one MATCHED product line;
    4. decided: its accepted decision resolved at least one line to a product.

    Whole-invoice operating-expense, accounting-resolution and fixed-asset reviews
    (their lines are INVALID_INPUT / account-resolved) never satisfy any of these.
    """

    if facts.had_product_reasons:
        return True
    if any(reason.code in PRODUCT_REVIEW_REASON_CODES for reason in facts.reasons):
        return True
    if facts.reasons_role is ReviewReasonsRole.DECISION_BASIS:
        return any(
            resolution.kind == _PRODUCT_RESOLUTION_KIND and resolution.product_id is not None
            for resolution in (facts.decision_resolutions or {}).values()
        )
    return any(match.status is ProductMatchStatus.MATCHED for match in (facts.evidence_lines or {}).values())


def _line_projection(
    facts: ProductLineFacts,
    line: SourceLine,
    *,
    number: str,
    sequence: int,
    supplier: int | None,
    reason: ManualReviewReasonCode | None,
    supplier_blocked: bool,
) -> WorkbenchProductLineProjection:
    seller_code = _text(line.seller_item_code)
    if facts.reasons_role is ReviewReasonsRole.DECISION_BASIS:
        state, product_id, matched_by = _decided_state(facts, number)
    else:
        state, product_id, matched_by = _pending_state(
            facts, number, supplier=supplier, reason=reason, supplier_blocked=supplier_blocked
        )
    return WorkbenchProductLineProjection(
        line_key=product_line_key(company_id=facts.company_id, review_id=facts.review_id, line_number=number),
        review_id=facts.review_id,
        company_id=facts.company_id,
        review_version=facts.review_version,
        line_number=number,
        line_sequence=sequence,
        supplier_partner_id=supplier,
        seller_item_code=seller_code,
        description=_text(line.description),
        quantity=line.quantity,
        unit_code=_text(line.unit_code),
        match_state=state,
        product_id=product_id,
        matched_by=matched_by,
        message=_message(
            state, seller_code=seller_code, decided=facts.reasons_role is ReviewReasonsRole.DECISION_BASIS
        ),
    )


def _pending_state(
    facts: ProductLineFacts,
    number: str,
    *,
    supplier: int | None,
    reason: ManualReviewReasonCode | None,
    supplier_blocked: bool,
) -> tuple[ProductLineMatchState, int | None, str | None]:
    if reason is not None:
        state = _REASON_STATES[reason]
        if state is ProductLineMatchState.PRODUCT_NOT_FOUND and supplier is None and supplier_blocked:
            # Supplier-scoped product resolution cannot run before the supplier is settled.
            state = ProductLineMatchState.SUPPLIER_UNRESOLVED
        return state, None, None
    match = (facts.evidence_lines or {}).get(number)
    if match is None:
        return ProductLineMatchState.NO_EVIDENCE, None, None
    if match.status is ProductMatchStatus.MATCHED and type(match.product_id) is int and match.product_id > 0:
        return ProductLineMatchState.MATCHED, match.product_id, _text(match.matched_by)
    state = _EVIDENCE_STATES.get(match.status, ProductLineMatchState.NO_EVIDENCE)
    if state is ProductLineMatchState.PRODUCT_NOT_FOUND and supplier is None:
        state = ProductLineMatchState.SUPPLIER_UNRESOLVED
    return state, None, None


def _decided_state(facts: ProductLineFacts, number: str) -> tuple[ProductLineMatchState, int | None, str | None]:
    resolution = (facts.decision_resolutions or {}).get(number)
    if resolution is None:
        return ProductLineMatchState.NO_EVIDENCE, None, None
    if resolution.kind == _PRODUCT_RESOLUTION_KIND:
        if type(resolution.product_id) is int and resolution.product_id > 0:
            return (
                ProductLineMatchState.MATCHED,
                resolution.product_id,
                _text(resolution.matched_by) or _text(resolution.product_source),
            )
        return ProductLineMatchState.NO_EVIDENCE, None, None
    if resolution.kind in _ACCOUNT_RESOLUTION_KINDS:
        return ProductLineMatchState.RESOLVED_WITHOUT_PRODUCT, None, None
    return ProductLineMatchState.NO_EVIDENCE, None, None


def _line_reasons(reasons: tuple[ManualReviewReason, ...]) -> dict[str, ManualReviewReasonCode]:
    """Line number -> its product reason; the most blocking reason wins on a repeat."""

    priority = (
        ManualReviewReasonCode.PRODUCT_IDENTIFIER_MISSING,
        ManualReviewReasonCode.PRODUCT_AMBIGUOUS,
        ManualReviewReasonCode.PRODUCT_NOT_FOUND,
    )
    found: dict[str, ManualReviewReasonCode] = {}
    for reason in reasons:
        number = (reason.line_number or "").strip()
        if reason.code not in PRODUCT_LINE_REASON_CODES or not number:
            continue
        current = found.get(number)
        if current is None or priority.index(reason.code) < priority.index(current):
            found[number] = reason.code
    return found


def _supplier_partner_id(match: SupplierMatch | None) -> int | None:
    if match is None or match.status is not PartnerMatchStatus.MATCHED:
        return None
    return match.partner_id if type(match.partner_id) is int and match.partner_id > 0 else None


def _message(state: ProductLineMatchState, *, seller_code: str | None, decided: bool) -> str:
    if state is ProductLineMatchState.MATCHED:
        return "Kabul edilen kararla ürün belirlendi." if decided else "Ürün eşleşti."
    if state is ProductLineMatchState.PRODUCT_NOT_FOUND:
        if seller_code is None:
            return "Ürün bulunamadı; satırda satıcı ürün kodu yok."
        return "Bu tedarikçi ve satıcı ürün kodu için ürün bulunamadı."
    if state is ProductLineMatchState.PRODUCT_AMBIGUOUS:
        return "Birden fazla ürün adayı var; otomatik seçim yapılmadı."
    if state is ProductLineMatchState.IDENTIFIER_MISSING:
        return "Satırda ürün tanımlayıcısı (satıcı kodu, barkod, dahili kod) yok."
    if state is ProductLineMatchState.SUPPLIER_UNRESOLVED:
        return "Tedarikçi kesinleşmeden ürün eşleştirilemez."
    if state is ProductLineMatchState.RESOLVED_WITHOUT_PRODUCT:
        return "Kabul edilen kararla hesap üzerinden çözüldü; ürün kullanılmadı."
    if decided:
        return "Kabul edilen kararda bu satır için ürün çözümü yok."
    return "Bu satır için güncel ürün eşleştirme kanıtı yok."


def _text(value: object) -> str | None:
    # Whitespace-strip only, exactly like ``normalize_seller_item_code`` / the matcher.
    if not isinstance(value, str):
        return None
    return value.strip() or None


__all__ = [
    "PRODUCT_LINE_KEY_PREFIX",
    "PRODUCT_REVIEW_REASON_CODES",
    "PRODUCT_LINE_STATE_LABELS",
    "ProductLineFacts",
    "ProductLineReadFacts",
    "ProductLineMatchState",
    "WorkbenchProductLineProjection",
    "build_product_line_projections",
    "line_sequences",
    "product_line_key",
]
