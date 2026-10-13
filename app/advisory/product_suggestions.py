"""PR F / F1: pure advisory product suggestions for one PRODUCT_NOT_FOUND invoice line.

Advisory only. Nothing here selects, maps or creates a product, writes supplierinfo,
changes an Internal Reference, or feeds the deterministic matcher, a decision, the
RESALE gate or product creation. The operator still chooses the product and presses
"Eşleştir"; the Hub re-validates that request exactly as before.

Pure: the caller passes immutable snapshots (catalog variants, supplier mappings,
category paths, earlier Hub mappings). There is no Odoo call, no database access and
no clock; the same inputs always give the same result, pinned to the line's review
version and to the catalog fingerprint.

Tiers, strongest first (a higher tier always ranks above a lower one):

* **T0 stale supplierinfo** -- ``product.supplierinfo`` for exactly this effective
  supplier and seller code exists *now* although the persisted evidence says
  PRODUCT_NOT_FOUND. The line would match deterministically after a controlled
  reclassification; the result says so but never reclassifies. T0 is returned alone.
* **T1 exact identifier** -- equality with a variant's Internal Reference of an
  identifier that is global by nature: the manufacturer item code, a full Microsoft
  CSP ``ProductId:SkuId`` seller code, or a ManageEngine SKU (seller code or standalone
  description token). An arbitrary supplier code is never compared with the global
  Internal Reference, and a CSP ProductId without the same SkuId is never enough.
* **T2 earlier same-supplier mapping** -- the Hub mapped the same supplier's same
  seller code or same description to a product before.
* **T3 guarded name similarity** -- token Jaccard and character similarity of the
  description and the product name, only with a meaningful (non-generic) shared token,
  at or above the threshold, and never for variant-catalog (ManageEngine) lines or
  candidates.

Hard discriminator guards (T2 and T3): an explicit conflicting value of product family,
edition, duration, capacity/metric or license model rejects the candidate outright --
confidence is never just lowered. So does an identifier conflict: when the line carries a
manufacturer-global identifier (CSP ``ProductId:SkuId``, ManageEngine SKU, manufacturer item
code), a candidate whose Internal Reference has the same shape but another value is a
different product (e.g. the same CSP ProductId with another SkuId). Candidates
within the tie margin of each other share a rank and are reported as ambiguous; no
winner is invented. At most ``max_suggestions`` (3) are returned; no safe candidate
means no suggestion.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from difflib import SequenceMatcher
from enum import StrEnum

from app.application.dto import ApplicationDTO
from app.application.workbench.product_line_creation_policy import (
    DESCRIPTION_SKU_TOKEN,
    code_key,
    compile_manual_only_sku_patterns,
)

#: Bumped whenever tiers, guards or scoring change: every persisted suggestion set of an
#: older engine version is regenerated.
ENGINE_VERSION = "f1.2"
DEFAULT_THRESHOLD = Decimal("0.55")
DEFAULT_TIE_MARGIN = Decimal("0.05")
DEFAULT_MAX_SUGGESTIONS = 3
EXACT_SCORE = Decimal("1.00")
PRIOR_MAPPING_SCORE = Decimal("0.90")
_TOKEN_WEIGHT = Decimal("0.6")
_CHAR_WEIGHT = Decimal("0.4")
_SCORE_QUANTUM = Decimal("0.01")

#: Microsoft CSP catalog identity ``ProductId:SkuId`` (e.g. ``CFQ7TTC0LH18:0001``).
CSP_IDENTIFIER = re.compile(r"^([A-Z0-9]{12}):([A-Z0-9]{4})$", re.IGNORECASE)

STALE_MAPPING_WARNING = (
    "Kayıtlı inceleme kanıtı eski: bu tedarikçi kodu artık bu ürüne bağlı. Kontrollü yeniden sınıflandırma "
    "gerekir; otomatik yapılmaz."
)
TIE_WARNING = "Benzer skorlu birden fazla aday var; doğru ürünü operatör seçmelidir."
CSP_PRODUCT_ONLY_NOTE = "Microsoft ürün kimliği (ProductId) eşleşti ancak SKU farklı; öneri yapılmadı."
VARIANT_CATALOG_NOTE = "Varyantlı lisans kataloğu satırı: benzerlik önerisi yapılmaz, yalnız kesin kod eşleşmesi."
AMBIGUOUS_SUPPLIERINFO_NOTE = (
    "Bu tedarikçi kodu için tek ve kesin bir tedarikçi ürün kaydı yok; deterministik öneri yok."
)
NEAR_MISS_SUPPLIERINFO_NOTE = (
    "Bu tedarikçinin ürün kaydındaki kod yalnız büyük/küçük harf veya boşlukla farklı; kesin kod eşleşmesi "
    "değil, yeniden sınıflandırma bu satırı eşleştirmez."
)


class SuggestionTier(StrEnum):
    T0_STALE_SUPPLIERINFO = "t0_stale_supplierinfo"
    T1_EXACT_IDENTIFIER = "t1_exact_identifier"
    T2_PRIOR_MAPPING = "t2_prior_mapping"
    T3_NAME_SIMILARITY = "t3_name_similarity"


_TIER_ORDER = {tier: index for index, tier in enumerate(SuggestionTier)}


@dataclass(frozen=True, slots=True)
class CatalogVariant(ApplicationDTO):
    """One ``product.product`` as read (read-only) from Odoo."""

    product_id: int
    template_id: int
    name: str
    default_code: str | None
    active: bool
    purchase_ok: bool
    category_id: int | None
    company_id: int | None = None


@dataclass(frozen=True, slots=True)
class CatalogSupplierMapping(ApplicationDTO):
    """One ``product.supplierinfo`` row (template-level when ``product_id`` is None)."""

    supplierinfo_id: int
    partner_id: int
    product_code: str | None
    template_id: int
    product_id: int | None = None
    company_id: int | None = None


@dataclass(frozen=True, slots=True)
class CatalogSnapshot(ApplicationDTO):
    variants: tuple[CatalogVariant, ...]
    supplier_mappings: tuple[CatalogSupplierMapping, ...] = ()
    #: ``(category id, complete_name)`` pairs, e.g. ``(7, "Software Licenses / ManageEngine / ...")``.
    category_paths: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True, slots=True)
class PriorMapping(ApplicationDTO):
    """A product the Hub itself mapped earlier for a line of the same supplier."""

    supplier_partner_id: int
    product_id: int
    seller_item_code: str | None = None
    description: str | None = None
    source: str = "hub"


@dataclass(frozen=True, slots=True)
class SuggestionLine(ApplicationDTO):
    review_id: str
    company_id: int
    review_version: int
    line_number: str
    supplier_partner_id: int | None
    seller_item_code: str | None
    description: str | None
    manufacturer_item_code: str | None = None


@dataclass(frozen=True, slots=True)
class SuggestionPolicy(ApplicationDTO):
    manual_only_supplier_ids: frozenset[int] = frozenset()
    manual_only_category_ids: frozenset[int] = frozenset()
    threshold: Decimal = DEFAULT_THRESHOLD
    tie_margin: Decimal = DEFAULT_TIE_MARGIN
    max_suggestions: int = DEFAULT_MAX_SUGGESTIONS


DEFAULT_POLICY = SuggestionPolicy()


@dataclass(frozen=True, slots=True)
class ProductSuggestion(ApplicationDTO):
    rank: int
    product_id: int
    tier: SuggestionTier
    score: Decimal
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    review_version: int
    catalog_fingerprint: str


@dataclass(frozen=True, slots=True)
class ProductSuggestionResult(ApplicationDTO):
    review_id: str
    line_number: str
    review_version: int
    catalog_fingerprint: str
    suggestions: tuple[ProductSuggestion, ...] = ()
    #: T0: a controlled MASTER_DATA_CHANGED reclassification would match the line; never executed here.
    reclassification_recommended: bool = False
    #: More than one suggestion shares rank 1; the operator must choose.
    ambiguous: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


# ------------------------------------------------------------------------------ public API


def variant_catalog_fingerprint(catalog: CatalogSnapshot) -> str:
    """Order-independent SHA-256 of the variants and categories the engine reads.

    Per variant: id, template, display name (variant attributes included), Internal
    Reference, category, active state, purchase eligibility and company.
    """

    return _digest(
        "variants",
        [[v.product_id, v.template_id, v.name, v.default_code, v.category_id, v.active, v.purchase_ok, v.company_id]
         for v in catalog.variants],
        [[category_id, path] for category_id, path in catalog.category_paths],
    )  # fmt: skip


def supplierinfo_fingerprint(catalog: CatalogSnapshot) -> str:
    """Order-independent SHA-256 of the supplier mappings (id, supplier, template, variant, code, company)."""

    return _digest(
        "supplierinfo",
        [[m.supplierinfo_id, m.partner_id, m.template_id, m.product_id, m.product_code, m.company_id]
         for m in catalog.supplier_mappings],
    )  # fmt: skip


def catalog_fingerprint(catalog: CatalogSnapshot) -> str:
    """Both fingerprints combined: the exact catalog state one suggestion was generated from."""

    return combine_fingerprints(variant_catalog_fingerprint(catalog), supplierinfo_fingerprint(catalog))


def combine_fingerprints(variant_catalog: str, supplierinfo: str) -> str:
    """The combined fingerprint from its two parts (persisted sets store the parts)."""

    return _digest("catalog", [[variant_catalog, supplierinfo]])


def _digest(kind: str, *row_groups: list[list[object]]) -> str:
    groups = [sorted(json.dumps(row, ensure_ascii=False, default=str) for row in rows) for rows in row_groups]
    encoded = json.dumps([kind, groups], ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def suggest_products(
    line: SuggestionLine,
    catalog: CatalogSnapshot,
    *,
    prior_mappings: Sequence[PriorMapping] = (),
    policy: SuggestionPolicy | None = None,
) -> ProductSuggestionResult:
    """Advisory shortlist for one PRODUCT_NOT_FOUND line. Never mutates its inputs."""

    policy = DEFAULT_POLICY if policy is None else policy
    fingerprint = catalog_fingerprint(catalog)
    context = _Context(line=line, catalog=catalog, policy=policy)
    notes: list[str] = []

    stale = _stale_supplierinfo(context, notes)
    if stale is not None:
        suggestion = ProductSuggestion(
            rank=1,
            product_id=stale.product_id,
            tier=SuggestionTier.T0_STALE_SUPPLIERINFO,
            score=EXACT_SCORE,
            reasons=stale.reasons,
            warnings=(STALE_MAPPING_WARNING,),
            review_version=line.review_version,
            catalog_fingerprint=fingerprint,
        )
        return ProductSuggestionResult(
            review_id=line.review_id,
            line_number=line.line_number,
            review_version=line.review_version,
            catalog_fingerprint=fingerprint,
            suggestions=(suggestion,),
            reclassification_recommended=True,
            notes=tuple(notes),
        )

    candidates = [
        *_exact_identifiers(context, notes),
        *_prior_mappings(context, prior_mappings),
        *_name_similarity(context, notes),
    ]
    suggestions, ambiguous = _rank(candidates, line=line, fingerprint=fingerprint, policy=policy, notes=notes)
    return ProductSuggestionResult(
        review_id=line.review_id,
        line_number=line.line_number,
        review_version=line.review_version,
        catalog_fingerprint=fingerprint,
        suggestions=suggestions,
        ambiguous=ambiguous,
        notes=tuple(notes),
    )


# ------------------------------------------------------------------------------ tiers


@dataclass(frozen=True, slots=True)
class _Candidate:
    product_id: int
    tier: SuggestionTier
    score: Decimal
    reasons: tuple[str, ...]


class _Context:
    def __init__(self, *, line: SuggestionLine, catalog: CatalogSnapshot, policy: SuggestionPolicy) -> None:
        self.line = line
        self.policy = policy
        self.eligible = tuple(
            sorted(
                (v for v in catalog.variants if _eligible(v, company_id=line.company_id)),
                key=lambda variant: variant.product_id,
            )
        )
        self.by_id = {variant.product_id: variant for variant in self.eligible}
        self.mappings = catalog.supplier_mappings
        self.paths = dict(catalog.category_paths)
        self.variants_by_template: dict[int, list[CatalogVariant]] = {}
        for variant in self.eligible:
            self.variants_by_template.setdefault(variant.template_id, []).append(variant)
        self.sku_patterns = compile_manual_only_sku_patterns()

    def is_variant_catalog_line(self) -> bool:
        line = self.line
        if line.supplier_partner_id is not None and line.supplier_partner_id in self.policy.manual_only_supplier_ids:
            return True
        if line.description and DESCRIPTION_SKU_TOKEN.search(line.description):
            return True
        seller = _clean(line.seller_item_code)
        return seller is not None and self.is_sku_shaped(seller)

    def is_sku_shaped(self, code: str) -> bool:
        return any(pattern.fullmatch(code) for pattern in self.sku_patterns)

    def identifier_conflict(self, variant: CatalogVariant) -> bool:
        """The line names a manufacturer-global identifier and the candidate carries a different one.

        A Microsoft CSP ``ProductId:SkuId``, a ManageEngine SKU or a manufacturer item code on
        the line is an explicit identity; a candidate whose Internal Reference has the same shape
        but another value (e.g. the same CSP ProductId with another SkuId) is a different product.
        """

        code = (variant.default_code or "").strip()
        if not code:
            return False
        line = self.line
        for identifier in (_clean(line.seller_item_code), _clean(line.manufacturer_item_code)):
            if identifier is None or code_key(identifier) == code_key(code):
                continue
            if CSP_IDENTIFIER.fullmatch(identifier) and CSP_IDENTIFIER.fullmatch(code):
                return True
            if self.is_sku_shaped(identifier) and self.is_sku_shaped(code):
                return True
        return False

    def is_variant_catalog_candidate(self, variant: CatalogVariant) -> bool:
        if variant.default_code and self.is_sku_shaped(variant.default_code.strip()):
            return True
        if variant.category_id is None:
            return False
        if variant.category_id in self.policy.manual_only_category_ids:
            return True
        path = self.paths.get(variant.category_id)
        return path is not None and any(
            (root := self.paths.get(root_id)) is not None and path.startswith(f"{root} / ")
            for root_id in self.policy.manual_only_category_ids
        )


@dataclass(frozen=True, slots=True)
class _Stale:
    product_id: int
    reasons: tuple[str, ...]


def _stale_supplierinfo(context: _Context, notes: list[str]) -> _Stale | None:
    """T0 sees exactly the rows the deterministic matcher's supplierinfo lookup would see.

    The matcher only strips the invoice code and asks Odoo for ``product_code = <code>``,
    which is exact and case-sensitive. A row whose code differs only by letter case or
    whitespace is never T0 and never a reclassification recommendation.
    """

    line = context.line
    seller = _clean(line.seller_item_code)
    if seller is None or line.supplier_partner_id is None:
        return None
    supplier_rows = [
        mapping
        for mapping in context.mappings
        if mapping.partner_id == line.supplier_partner_id
        and mapping.product_code is not None
        and mapping.company_id in (None, line.company_id)
    ]
    rows = [mapping for mapping in supplier_rows if mapping.product_code == seller]
    if not rows:
        if any(code_key(mapping.product_code or "") == code_key(seller) for mapping in supplier_rows):
            notes.append(NEAR_MISS_SUPPLIERINFO_NOTE)
        return None
    variant = _single_variant(context, rows[0]) if len(rows) == 1 else None
    if variant is None:
        notes.append(AMBIGUOUS_SUPPLIERINFO_NOTE)
        return None
    return _Stale(
        product_id=variant.product_id,
        reasons=(
            f"Tedarikçi ürün kaydı #{rows[0].supplierinfo_id}: tedarikçi #{line.supplier_partner_id}, kod {seller}",
        ),
    )


def _single_variant(context: _Context, mapping: CatalogSupplierMapping) -> CatalogVariant | None:
    if mapping.product_id is not None:
        variant = context.by_id.get(mapping.product_id)
        return variant if variant is not None and variant.template_id == mapping.template_id else None
    variants = context.variants_by_template.get(mapping.template_id, [])
    return variants[0] if len(variants) == 1 else None


def _exact_identifiers(context: _Context, notes: list[str]) -> list[_Candidate]:
    line = context.line
    identifiers: list[tuple[str, str]] = []
    manufacturer = _clean(line.manufacturer_item_code)
    if manufacturer is not None:
        identifiers.append(("üretici kodu", manufacturer))
    seller = _clean(line.seller_item_code)
    if seller is not None and (CSP_IDENTIFIER.fullmatch(seller) or context.is_sku_shaped(seller)):
        identifiers.append(("üretici kimliği biçimli satıcı kodu", seller))
    if line.description:
        for match in DESCRIPTION_SKU_TOKEN.finditer(line.description):
            identifiers.append(("açıklamadaki SKU", match.group(0)))

    found: list[_Candidate] = []
    for label, identifier in identifiers:
        key = code_key(identifier)
        for variant in context.eligible:
            if variant.default_code and code_key(variant.default_code) == key:
                found.append(
                    _Candidate(
                        product_id=variant.product_id,
                        tier=SuggestionTier.T1_EXACT_IDENTIFIER,
                        score=EXACT_SCORE,
                        reasons=(f"Kesin kod eşleşmesi ({label}): {identifier}",),
                    )
                )
    if seller is not None and (csp := CSP_IDENTIFIER.fullmatch(seller)) is not None and not found:
        product_id = csp.group(1).casefold()
        if any(
            variant.default_code
            and (other := CSP_IDENTIFIER.fullmatch(variant.default_code.strip())) is not None
            and other.group(1).casefold() == product_id
            for variant in context.eligible
        ):
            notes.append(CSP_PRODUCT_ONLY_NOTE)
    return found


def _prior_mappings(context: _Context, prior_mappings: Sequence[PriorMapping]) -> list[_Candidate]:
    line = context.line
    if line.supplier_partner_id is None:
        return []
    seller = _clean(line.seller_item_code)
    description = _normalize(line.description)
    found: list[_Candidate] = []
    for prior in sorted(prior_mappings, key=lambda item: (item.product_id, item.source)):
        if prior.supplier_partner_id != line.supplier_partner_id:
            continue
        variant = context.by_id.get(prior.product_id)
        if variant is None:
            continue
        prior_seller = _clean(prior.seller_item_code)
        same_code = seller is not None and prior_seller is not None and code_key(seller) == code_key(prior_seller)
        same_description = bool(description) and description == _normalize(prior.description)
        if not (same_code or same_description):
            continue
        if context.identifier_conflict(variant) or _conflicts(line.description, variant.name):
            continue
        basis = "aynı satıcı kodu" if same_code else "aynı açıklama"
        found.append(
            _Candidate(
                product_id=variant.product_id,
                tier=SuggestionTier.T2_PRIOR_MAPPING,
                score=PRIOR_MAPPING_SCORE,
                reasons=(f"Aynı tedarikçide daha önce eşleştirildi ({basis}; kaynak: {prior.source})",),
            )
        )
    return found


def _name_similarity(context: _Context, notes: list[str]) -> list[_Candidate]:
    line = context.line
    if not line.description:
        return []
    if context.is_variant_catalog_line():
        notes.append(VARIANT_CATALOG_NOTE)
        return []
    description = _normalize(line.description)
    line_tokens = _tokens(description)
    line_meaningful = _meaningful(line_tokens)
    if not line_meaningful:
        return []
    line_attributes = _discriminators(description)
    found: list[_Candidate] = []
    rejected = 0
    for variant in context.eligible:
        if context.is_variant_catalog_candidate(variant):
            continue
        name = _normalize(_strip_code_prefix(variant.name))
        name_tokens = _tokens(name)
        shared = line_meaningful & _meaningful(name_tokens)
        if not shared:
            continue
        if context.identifier_conflict(variant) or _attribute_conflict(line_attributes, _discriminators(name)):
            rejected += 1
            continue
        jaccard = Decimal(len(line_tokens & name_tokens)) / Decimal(len(line_tokens | name_tokens))
        ratio = Decimal(str(SequenceMatcher(None, description, name).ratio()))
        score = (_TOKEN_WEIGHT * jaccard + _CHAR_WEIGHT * ratio).quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_UP)
        if score < context.policy.threshold:
            continue
        found.append(
            _Candidate(
                product_id=variant.product_id,
                tier=SuggestionTier.T3_NAME_SIMILARITY,
                score=score,
                reasons=(
                    f"Ad benzerliği {score}",
                    "Ortak anlamlı sözcükler: " + ", ".join(sorted(shared)),
                ),
            )
        )
    if rejected:
        notes.append(
            f"{rejected} aday ayırt edici bir özellik çakışması (aile/sürüm/süre/kapasite/lisans/kimlik) "
            "nedeniyle elendi."
        )
    return found


# ------------------------------------------------------------------------------ ranking


def _rank(
    candidates: Iterable[_Candidate],
    *,
    line: SuggestionLine,
    fingerprint: str,
    policy: SuggestionPolicy,
    notes: list[str],
) -> tuple[tuple[ProductSuggestion, ...], bool]:
    best: dict[int, _Candidate] = {}
    for candidate in candidates:
        current = best.get(candidate.product_id)
        if current is None or (_TIER_ORDER[candidate.tier], -candidate.score) < (
            _TIER_ORDER[current.tier],
            -current.score,
        ):
            best[candidate.product_id] = candidate
    ordered = sorted(best.values(), key=lambda c: (_TIER_ORDER[c.tier], -c.score, c.product_id))
    if not ordered:
        return (), False

    ranks: list[int] = []
    head: _Candidate | None = None
    for position, candidate in enumerate(ordered, start=1):
        if head is not None and candidate.tier == head.tier and head.score - candidate.score <= policy.tie_margin:
            ranks.append(ranks[-1])
            continue
        head = candidate
        ranks.append(position)
    tied_top = sum(1 for rank in ranks if rank == 1)
    ambiguous = tied_top > 1
    if len(ordered) > policy.max_suggestions:
        notes.append(f"{len(ordered)} uygun aday var; en güçlü {policy.max_suggestions} tanesi gösterildi.")
    suggestions = tuple(
        ProductSuggestion(
            rank=rank,
            product_id=candidate.product_id,
            tier=candidate.tier,
            score=candidate.score,
            reasons=candidate.reasons,
            warnings=(TIE_WARNING,) if sum(1 for other in ranks if other == rank) > 1 else (),
            review_version=line.review_version,
            catalog_fingerprint=fingerprint,
        )
        for candidate, rank in list(zip(ordered, ranks, strict=True))[: policy.max_suggestions]
    )
    return suggestions, ambiguous


# ------------------------------------------------------------------------------ text normalization

_TURKISH = str.maketrans({"İ": "i", "I": "i", "ı": "i"})
_SYNONYMS = {"standart": "standard", "lisansi": "lisans", "kullanicilar": "kullanici"}
#: Words that never make two products "the same" on their own.
GENERIC_TOKENS = frozenset(
    {
        "ms", "microsoft", "server", "servers", "lisans", "license", "licence", "licenses", "licensing",
        "edition", "per", "ve", "and", "the", "for", "of", "ile", "adet", "hizmet", "hizmeti", "hizmetleri",
        "service", "services", "cloud", "bulut", "plan", "urun", "product", "paket", "package", "pack",
        "yazilim", "software", "capacity", "kapasite", "azure", "business", "bedeli", "ucret", "fee", "fees",
    }
)  # fmt: skip
_FAMILY = {
    "windows": "windows", "sql": "sql", "office": "office", "exchange": "exchange", "sharepoint": "sharepoint",
    "teams": "teams", "dynamics": "dynamics", "fabric": "fabric", "github": "github", "copilot": "copilot",
    "visio": "visio", "defender": "defender", "intune": "intune", "entra": "entra", "rds": "rds", "365": "m365",
    "m365": "m365", "powerbi": "powerbi",
}  # fmt: skip
_EDITION = {
    "standard": "standard", "datacenter": "datacenter", "enterprise": "enterprise", "professional": "professional",
    "basic": "basic", "premium": "premium", "essentials": "essentials", "ultimate": "ultimate",
    "starter": "starter", "advanced": "advanced",
}  # fmt: skip
_LICENSE_MODEL = {
    "subscription": "subscription", "abonelik": "subscription", "perpetual": "perpetual", "kalici": "perpetual",
    "maintenance": "maintenance", "bakim": "maintenance", "payg": "payg", "reservation": "reservation",
    "reserved": "reservation", "rezervasyon": "reservation",
}  # fmt: skip
_UNITS = {
    "user": "user", "users": "user", "kullanici": "user", "endpoint": "endpoint", "endpoints": "endpoint",
    "device": "device", "devices": "device", "cihaz": "device", "core": "core", "cores": "core",
    "cekirdek": "core", "vm": "vm", "vms": "vm", "technician": "technician", "technicians": "technician",
    "node": "node", "nodes": "node", "seat": "seat", "seats": "seat", "mailbox": "mailbox", "mailboxes": "mailbox",
}  # fmt: skip
_ATTRIBUTE_WORDS = frozenset(
    {*_EDITION, *_LICENSE_MODEL, *_UNITS, "year", "years", "yil", "yillik", "month", "months", "monthly", "aylik",
     "annual", "yearly", "pay", "as", "you", "go"}
)  # fmt: skip
_COUNT_UNIT = re.compile(r"\b(\d+)\s*(" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")\b")
_PER_UNIT = re.compile(r"\bper\s+(" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")\b")
_FABRIC_SKU = re.compile(r"\bf(\d+)\b")
_YEARS = re.compile(r"\b(\d+)\s*(?:y|yr|yrs|year|years|yil|yillik)\b")
_RESERVED_YEARS = re.compile(r"\bri(\d+)y\b")
_MONTHS = re.compile(r"\b(\d+)\s*(?:month|months|ay|aylik)\b")


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _strip_code_prefix(name: str) -> str:
    return re.sub(r"^\s*\[[^\]]*\]\s*", "", name)


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text.translate(_TURKISH))
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char)).lower()
    ascii_text = re.sub(r"\bpay\s*-?\s*as\s*-?\s*you\s*-?\s*go\b", "payg", ascii_text)
    ascii_text = re.sub(r"\bpower\s+bi\b", "powerbi", ascii_text)
    ascii_text = re.sub(r"\bremote\s+desktop\b", "rds", ascii_text)
    words = re.findall(r"[a-z0-9]+", ascii_text)
    return " ".join(_SYNONYMS.get(word, word) for word in words)


def _tokens(normalized: str) -> frozenset[str]:
    return frozenset(token for token in normalized.split() if token not in GENERIC_TOKENS)


def _meaningful(tokens: frozenset[str]) -> frozenset[str]:
    return frozenset(
        token
        for token in tokens
        if len(token) >= 3
        and token not in _ATTRIBUTE_WORDS
        and not token.isdigit()
        and not _FABRIC_SKU.fullmatch(token)
        and not re.fullmatch(r"(?:ri)?\d+[ym]", token)
    )


@dataclass(frozen=True, slots=True)
class _Discriminators:
    family: frozenset[str]
    edition: frozenset[str]
    duration: frozenset[str]
    license_model: frozenset[str]
    #: unit -> explicit counts (empty when only the metric, e.g. "per VM", is stated).
    capacity: tuple[tuple[str, frozenset[int]], ...]


def _discriminators(normalized: str) -> _Discriminators:
    words = normalized.split()
    duration = {f"{n}y" for n in _YEARS.findall(normalized)} | {f"{n}y" for n in _RESERVED_YEARS.findall(normalized)}
    duration |= {f"{n}m" for n in _MONTHS.findall(normalized)}
    if any(word in ("annual", "yearly", "yillik") for word in words) and not duration:
        duration.add("1y")
    if any(word in ("monthly", "aylik") for word in words) and not duration:
        duration.add("1m")
    license_model = {_LICENSE_MODEL[word] for word in words if word in _LICENSE_MODEL}
    if _RESERVED_YEARS.search(normalized):
        license_model.add("reservation")
    capacity: dict[str, set[int]] = {}
    for count, unit in _COUNT_UNIT.findall(normalized):
        capacity.setdefault(_UNITS[unit], set()).add(int(count))
    for unit in _PER_UNIT.findall(normalized):
        capacity.setdefault(_UNITS[unit], set())
    for count in _FABRIC_SKU.findall(normalized):
        capacity.setdefault("fabric_sku", set()).add(int(count))
    return _Discriminators(
        family=frozenset(_FAMILY[word] for word in words if word in _FAMILY),
        edition=frozenset(_EDITION[word] for word in words if word in _EDITION),
        duration=frozenset(duration),
        license_model=frozenset(license_model),
        capacity=tuple(sorted((unit, frozenset(counts)) for unit, counts in capacity.items())),
    )


def _attribute_conflict(left: _Discriminators, right: _Discriminators) -> bool:
    for mine, theirs in (
        (left.family, right.family),
        (left.edition, right.edition),
        (left.duration, right.duration),
        (left.license_model, right.license_model),
    ):
        if mine and theirs and mine != theirs:
            return True
    mine_capacity, their_capacity = dict(left.capacity), dict(right.capacity)
    if mine_capacity and their_capacity:
        shared_units = mine_capacity.keys() & their_capacity.keys()
        if not shared_units:
            return True
        for unit in shared_units:
            if mine_capacity[unit] and their_capacity[unit] and mine_capacity[unit] != their_capacity[unit]:
                return True
    return False


def _conflicts(description: str | None, product_name: str) -> bool:
    return _attribute_conflict(
        _discriminators(_normalize(description)), _discriminators(_normalize(_strip_code_prefix(product_name)))
    )


def _eligible(variant: CatalogVariant, *, company_id: int) -> bool:
    return variant.active and variant.purchase_ok and variant.company_id in (None, company_id)


__all__ = [
    "CSP_IDENTIFIER",
    "CatalogSnapshot",
    "CatalogSupplierMapping",
    "CatalogVariant",
    "DEFAULT_POLICY",
    "ENGINE_VERSION",
    "GENERIC_TOKENS",
    "PriorMapping",
    "ProductSuggestion",
    "ProductSuggestionResult",
    "SuggestionLine",
    "SuggestionPolicy",
    "SuggestionTier",
    "catalog_fingerprint",
    "combine_fingerprints",
    "supplierinfo_fingerprint",
    "variant_catalog_fingerprint",
    "suggest_products",
]
