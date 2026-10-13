"""PR D: fail-closed product master-data policy for Workbench new-product creation.

Decides only; every check is read-only and runs before any Odoo write. Grounded in the
ICT Product Master Data Policy draft (2026-09-16 production audit):

* **Supplier code is never Internal Reference** (§4/§5/§18). The seller code goes to
  ``product.supplierinfo.product_code`` only. An Internal Reference equal (ignoring case and
  whitespace) to this line's seller code, to any other seller code on the same invoice, or
  to any vendor code already stored on an Odoo ``product.supplierinfo`` is refused, so a
  mechanical copy of a vendor code cannot slip through.
* **Variant/license catalogs are manual-only** (§12/§14/§17). ManageEngine license SKUs
  (``85710.1SS3``, ``67005.6MPMFA4``, ``702012.0SUP6`` ...) live on product *variants*
  with License Model / Pack / Users attributes; a single-variant product created here
  would be wrong master data. Detection is structural, never a vendor-name allowlist:
  a built-in SKU shape plus configured extra shapes, configured manual-only category
  subtrees (e.g. "Software Licenses / ManageEngine"), and any existing Odoo variant
  whose Internal Reference already equals the seller code.
* **Manual-only suppliers** (PR D.1). Every line whose effective supplier is, belongs to,
  or shares the VAT of a ``PRODUCT_CREATION_MANUAL_ONLY_SUPPLIER_IDS`` partner (production:
  VİTEL, ICT's ManageEngine distributor) is manual-only. The supplier is re-read from Odoo:
  the partner itself, its ``parent_id`` and its ``commercial_partner_id`` are compared, so a
  child contact or invoice address of the distributor is caught; the source invoice's
  supplier VAT is compared with the configured partners' VATs, so a duplicate partner of the
  same company is caught too. Anything unreadable fails closed.
* **SKU in the description** (PR D.1). VİTEL invoices carry the ManageEngine SKU in the line
  description and an unrelated numeric code as the seller code, so the built-in SKU shapes
  are also searched for as a standalone token in the description. The description token
  requires at least one letter after the dot, so amounts like ``12500.00`` never match.
* **Category, type and UoM are operator choices** (§6/§7/§8). Category is required and
  validated by the existing CREATE_NEW_PRODUCT category policy (RESALE: exact allowlist,
  non-storable, valid purchase account). UoM must be in ``PRODUCT_CREATION_UOM_IDS``; no
  UoM is assumed. When ``PRODUCT_CREATION_UOM_UNIT_CODES`` lists invoice unit codes for the
  chosen UoM, the source line's UN/CEFACT unit code must be one of them, so a "Days"
  product is never created from a line invoiced per piece.
* **No duplicates.** An active product with the same name, or any product already
  carrying the requested Internal Reference, is refused -- map the existing product
  instead ("Eşleştir").
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.application.commands.product_remediation import ValidatedProductCategory
from app.application.workbench.product_line_creation import (
    NewProductIntent,
    ProductLineCreateDuplicateError,
    ProductLineCreateManualOnlyError,
    ProductLineCreatePolicyError,
)
from app.application.workbench.product_remediation_category import (
    CategoryPurchaseAccountLister,
    ProductRemediationCategoryPolicy,
)
from app.application.workbench.purchase_account_discovery import (
    CategoryPurchaseAccountConfiguration,
    ListCategoryPurchaseAccountsQuery,
)
from app.erp.exceptions import ErpRepositoryError
from app.erp.models import Partner

#: Structural shapes of vendor license SKUs that belong to multi-variant catalogs.
#: Always active; ``PRODUCT_CREATION_MANUAL_ONLY_SKU_PATTERNS`` can only add to them.
BUILTIN_MANUAL_ONLY_SKU_PATTERNS: tuple[str, ...] = (
    # ManageEngine license SKUs: <4-6 digits>.<digit><2-8 alphanumerics>.
    r"^\d{4,6}\.\d[0-9A-Z]{2,8}$",
    # PR D.1: single-character suffixes too (production variants 67225.5M, 85610.0N), when
    # the suffix holds a letter (a plain decimal like 1500.0 is never an SKU).
    r"^\d{4,6}\.\d(?=[0-9A-Z]*[A-Z])[0-9A-Z]{1,8}$",
)
#: PR D.1: the same SKU shape as a standalone token inside free text (line descriptions).
#: A letter after the dot is required, and the token may not touch other code characters.
DESCRIPTION_SKU_TOKEN = re.compile(
    r"(?<![0-9A-Z.])\d{4,6}\.\d(?=[0-9A-Z]*[A-Z])[0-9A-Z]{1,8}(?![0-9A-Z.])", re.IGNORECASE
)

MANUAL_ONLY_SKU_MESSAGE = (
    "'{code}' varyantlı bir lisans kataloğu SKU'su (ör. ManageEngine); Workbench'ten yeni ürün oluşturulamaz. "
    "Ürünü Odoo'da lisans modeli/paket varyantlarıyla manuel oluşturun, sonra satırı 'Eşleştir' ile eşleyin."
)
MANUAL_ONLY_REFERENCE_MESSAGE = (
    "İç referans '{code}' bir lisans kataloğu SKU'su biçiminde; bu kodlar ürün varyantında manuel tanımlanır. "
    "Kısa bir ICT kodu girin veya boş bırakın."
)
REFERENCE_IS_SUPPLIER_CODE_MESSAGE = (
    "İç referans, tedarikçinin ürün koduyla ({code}) aynı olamaz. Tedarikçi kodu yalnız tedarikçi ürün kaydına "
    "yazılır; kısa bir ICT kodu girin veya iç referansı boş bırakın."
)
MANUAL_ONLY_DESCRIPTION_MESSAGE = (
    "Satır açıklamasında varyantlı bir lisans kataloğu SKU'su var ('{code}', ör. ManageEngine); Workbench'ten yeni "
    "ürün oluşturulamaz. Ürünü Odoo'da varyantlarıyla manuel oluşturun, sonra satırı 'Eşleştir' ile eşleyin."
)
MANUAL_ONLY_SUPPLIER_MESSAGE = (
    "Bu tedarikçinin (#{partner}) ürünleri varyantlı lisans kataloğu ürünleridir (ör. ManageEngine); Workbench'ten "
    "yeni ürün oluşturulamaz. Ürünü Odoo'da manuel oluşturun, sonra satırı 'Eşleştir' ile eşleyin."
)
SUPPLIER_UNVERIFIABLE_MESSAGE = (
    "Tedarikçinin manuel-ürün kısıtı doğrulanamadı ({reason}); yeni ürün oluşturulmadı. Teknik destek alın."
)
SKU_POLICY_FAILED_MESSAGE = "Ürün kodu politikası değerlendirilemedi; yeni ürün oluşturulmadı. Teknik destek alın."
MANUAL_ONLY_CATEGORY_MESSAGE = (
    "Seçilen kategori ({category}) varyantlı lisans ürünleri için ayrılmış; bu kategoride ürün Odoo'da manuel "
    "oluşturulmalıdır."
)


class ProductCatalogReader(Protocol):
    """Read-only Odoo catalog lookups for duplicate prevention (company scope incl. shared)."""

    def find_variant_ids_by_default_code(self, default_code: str, *, company_id: int) -> tuple[int, ...]:
        """Variants (active AND archived) whose Internal Reference equals ``default_code``, ignoring case."""

    def find_template_ids_by_name(self, name: str, *, company_id: int) -> tuple[int, ...]:
        """Active templates whose name equals ``name`` exactly, ignoring case."""

    def find_supplierinfo_ids_by_product_code(self, product_code: str, *, company_id: int) -> tuple[int, ...]:
        """Supplier mappings of ANY vendor whose vendor product code equals ``product_code``, ignoring case."""


class SupplierPartnerLookup(Protocol):
    """Read-only ``res.partner`` by id, archived rows included (the existing PR A reader)."""

    def find_by_ids_including_archived(self, ids: Sequence[int]) -> Sequence[Partner]: ...


@dataclass(frozen=True, slots=True)
class ValidatedNewProduct:
    category: ValidatedProductCategory
    resale: bool


class ProductLineCreationPolicy:
    def __init__(
        self,
        *,
        category_policy: ProductRemediationCategoryPolicy,
        category_lister: CategoryPurchaseAccountLister,
        catalog_reader: ProductCatalogReader,
        allowed_uom_ids: Iterable[int],
        manual_only_category_ids: Iterable[int] = (),
        extra_manual_only_sku_patterns: Iterable[str] = (),
        uom_unit_codes: Mapping[int, Iterable[str]] | None = None,
        manual_only_supplier_ids: Iterable[int] = (),
        supplier_reader: SupplierPartnerLookup | None = None,
    ) -> None:
        self._category_policy = category_policy
        self._category_lister = category_lister
        self._catalog = catalog_reader
        self._allowed_uom_ids = frozenset(allowed_uom_ids)
        self._manual_only_category_ids = frozenset(manual_only_category_ids)
        self._sku_patterns = compile_manual_only_sku_patterns(extra_manual_only_sku_patterns)
        self._uom_unit_codes = {
            uom_id: frozenset(_unit_code(code) for code in codes) for uom_id, codes in (uom_unit_codes or {}).items()
        }
        self._manual_only_supplier_ids = frozenset(manual_only_supplier_ids)
        self._supplier_reader = supplier_reader

    def validate(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
        seller_item_code: str,
        intent: NewProductIntent,
        source_unit_code: str | None = None,
        invoice_seller_codes: Iterable[str] = (),
        supplier_partner_id: int | None = None,
        source_supplier_tax_number: str | None = None,
        source_description: str | None = None,
    ) -> ValidatedNewProduct:
        self.require_supplier_not_manual_only(
            supplier_partner_id=supplier_partner_id, source_supplier_tax_number=source_supplier_tax_number
        )
        self.require_codes_allowed(
            seller_item_code=seller_item_code,
            intent=intent,
            invoice_seller_codes=invoice_seller_codes,
            source_description=source_description,
        )
        self._require_allowed_uom(intent.uom_id, source_unit_code)
        resale = self._category_policy.purpose_is_resale(
            review_id=review_id, company_id=company_id, review_version=review_version
        )
        category = self._category_policy.validate_before_write(
            company_id=company_id, categ_id=intent.categ_id, is_storable=intent.is_storable, resale=resale
        )
        if category is None:  # pragma: no cover - categ_id is always set by NewProductIntent
            raise ProductLineCreatePolicyError("Ürün kategorisi seçilmelidir.")
        self._require_category_not_manual_only(company_id=company_id, categ_id=intent.categ_id)
        self._require_no_catalog_duplicate(company_id=company_id, seller_item_code=seller_item_code, intent=intent)
        return ValidatedNewProduct(category=category, resale=resale)

    # ------------------------------------------------------------------ pure SKU rules

    def require_codes_allowed(
        self,
        *,
        seller_item_code: str,
        intent: NewProductIntent,
        invoice_seller_codes: Iterable[str] = (),
        source_description: str | None = None,
    ) -> None:
        try:
            seller_code_is_sku = self._is_manual_only_sku(seller_item_code)
            description_sku = _description_sku(source_description)
        except Exception as exc:  # noqa: BLE001 - any evaluation failure must refuse, never allow
            raise ProductLineCreatePolicyError(SKU_POLICY_FAILED_MESSAGE) from exc
        if seller_code_is_sku:
            raise ProductLineCreateManualOnlyError(MANUAL_ONLY_SKU_MESSAGE.format(code=seller_item_code))
        if description_sku is not None:
            raise ProductLineCreateManualOnlyError(MANUAL_ONLY_DESCRIPTION_MESSAGE.format(code=description_sku))
        reference = intent.internal_reference
        if reference is None:
            return
        for vendor_code in (seller_item_code, *invoice_seller_codes):
            if code_key(reference) == code_key(vendor_code):
                raise ProductLineCreatePolicyError(REFERENCE_IS_SUPPLIER_CODE_MESSAGE.format(code=vendor_code))
        if self._is_manual_only_sku(reference):
            raise ProductLineCreateManualOnlyError(MANUAL_ONLY_REFERENCE_MESSAGE.format(code=reference))

    def _is_manual_only_sku(self, code: str) -> bool:
        compact = "".join(code.split())
        return any(pattern.fullmatch(compact) for pattern in self._sku_patterns)

    def _require_allowed_uom(self, uom_id: int, source_unit_code: str | None) -> None:
        if uom_id not in self._allowed_uom_ids:
            allowed = ", ".join(f"#{value}" for value in sorted(self._allowed_uom_ids)) or "yok"
            raise ProductLineCreatePolicyError(
                f"Seçilen ölçü birimi (#{uom_id}) yeni ürünler için onaylı değil; onaylı birimler: {allowed} "
                "(PRODUCT_CREATION_UOM_IDS)."
            )
        codes = self._uom_unit_codes.get(uom_id)
        if codes is not None and _unit_code(source_unit_code or "") not in codes:
            raise ProductLineCreatePolicyError(
                f"Seçilen ölçü birimi (#{uom_id}) faturadaki birimle ({source_unit_code or 'yok'}) uyumlu değil; "
                "doğru birimi seçin veya teknik destek alın."
            )

    # ------------------------------------------------------------------ Odoo reads

    def require_supplier_not_manual_only(
        self, *, supplier_partner_id: int | None, source_supplier_tax_number: str | None
    ) -> None:
        """PR D.1: refuse every line of a configured manual-only supplier, fail closed.

        Matches when the effective partner, its ``parent_id`` or its ``commercial_partner_id``
        is configured, or when the source invoice's supplier VAT / the partner's or its
        commercial partner's VAT equals a configured partner's VAT. A transient Odoo read
        error propagates (the request is retried later; nothing is created meanwhile).
        """

        configured = self._manual_only_supplier_ids
        if not configured:
            return
        if self._supplier_reader is None:
            raise ProductLineCreatePolicyError(SUPPLIER_UNVERIFIABLE_MESSAGE.format(reason="okuyucu yok"))
        if type(supplier_partner_id) is not int or supplier_partner_id <= 0:
            raise ProductLineCreatePolicyError(SUPPLIER_UNVERIFIABLE_MESSAGE.format(reason="tedarikçi belirsiz"))
        partners = self._read_partners({supplier_partner_id, *configured})
        effective = partners.get(supplier_partner_id)
        if effective is None or effective.commercial_partner_id is None:
            raise ProductLineCreatePolicyError(
                SUPPLIER_UNVERIFIABLE_MESSAGE.format(reason=f"tedarikçi #{supplier_partner_id} okunamadı")
            )
        missing = sorted(partner_id for partner_id in configured if partner_id not in partners)
        if missing:
            raise ProductLineCreatePolicyError(
                SUPPLIER_UNVERIFIABLE_MESSAGE.format(reason=f"yapılandırılmış tedarikçi {missing} Odoo'da yok")
            )
        commercial_id = effective.commercial_partner_id
        if commercial_id not in partners:
            partners.update(self._read_partners({commercial_id}))
        commercial = partners.get(commercial_id)
        if commercial is None:
            raise ProductLineCreatePolicyError(
                SUPPLIER_UNVERIFIABLE_MESSAGE.format(reason=f"ticari tedarikçi #{commercial_id} okunamadı")
            )
        identities = {effective.id, commercial.id} | ({effective.parent_id} if effective.parent_id else set())
        if identities & configured:
            raise ProductLineCreateManualOnlyError(MANUAL_ONLY_SUPPLIER_MESSAGE.format(partner=commercial.id))
        configured_vats = {vat for partner_id in configured if (vat := vat_key(partners[partner_id].tax_number))}
        candidate_vats = {
            vat_key(source_supplier_tax_number),
            vat_key(effective.tax_number),
            vat_key(commercial.tax_number),
        }
        if configured_vats & (candidate_vats - {None}):
            raise ProductLineCreateManualOnlyError(MANUAL_ONLY_SUPPLIER_MESSAGE.format(partner=commercial.id))

    def _read_partners(self, ids: set[int]) -> dict[int, Partner]:
        assert self._supplier_reader is not None
        try:
            records = self._supplier_reader.find_by_ids_including_archived(sorted(ids))
        except ErpRepositoryError:
            raise
        except Exception as exc:  # noqa: BLE001 - an unexpected reader failure refuses, never allows
            raise ProductLineCreatePolicyError(SUPPLIER_UNVERIFIABLE_MESSAGE.format(reason="okuma hatası")) from exc
        return {record.id: record for record in records if type(record.id) is int}

    def _require_category_not_manual_only(self, *, company_id: int, categ_id: int) -> None:
        if not self._manual_only_category_ids:
            return
        categories = {
            category.category_id: category
            for category in self._category_lister.execute(ListCategoryPurchaseAccountsQuery(company_id=company_id))
        }
        target = categories.get(categ_id)
        roots = [categories.get(root_id) for root_id in sorted(self._manual_only_category_ids)]
        if target is None or any(root is None for root in roots):
            raise ProductLineCreatePolicyError(
                "Kategori kısıtları doğrulanamadı (kategori bulunamadı); ürün oluşturulmadı. Teknik destek alın."
            )
        for root in roots:
            assert root is not None
            if _within(target, root):
                raise ProductLineCreateManualOnlyError(
                    MANUAL_ONLY_CATEGORY_MESSAGE.format(category=target.category_complete_name or target.category_name)
                )

    def _require_no_catalog_duplicate(
        self, *, company_id: int, seller_item_code: str, intent: NewProductIntent
    ) -> None:
        coded = self._catalog.find_variant_ids_by_default_code(seller_item_code, company_id=company_id)
        if coded:
            raise ProductLineCreateDuplicateError(
                f"'{seller_item_code}' kodu Odoo'da zaten bir ürünün iç referansı ({_ids(coded)}); yeni ürün "
                "oluşturulmadı. Doğru ürünse satırı 'Eşleştir' ile eşleyin."
            )
        if intent.internal_reference is not None:
            referenced = self._catalog.find_variant_ids_by_default_code(
                intent.internal_reference, company_id=company_id
            )
            if referenced:
                raise ProductLineCreateDuplicateError(
                    f"İç referans '{intent.internal_reference}' Odoo'da zaten kullanılıyor ({_ids(referenced)}); "
                    "yeni ürün oluşturulmadı."
                )
            vendor_coded = self._catalog.find_supplierinfo_ids_by_product_code(
                intent.internal_reference, company_id=company_id
            )
            if vendor_coded:
                raise ProductLineCreatePolicyError(
                    f"İç referans '{intent.internal_reference}' Odoo'da bir tedarikçinin ürün kodu olarak kayıtlı "
                    f"(tedarikçi ürün kaydı {_ids(vendor_coded)}); tedarikçi kodları iç referans olamaz."
                )
        named = self._catalog.find_template_ids_by_name(intent.product_name, company_id=company_id)
        if named:
            raise ProductLineCreateDuplicateError(
                f"Odoo'da '{intent.product_name}' adlı aktif bir ürün zaten var ({_ids(named)}); yeni ürün "
                "oluşturulmadı. Aynı ürünse 'Eşleştir' ile eşleyin, farklıysa ayırt edici bir ad girin."
            )


def compile_manual_only_sku_patterns(extra: Iterable[str] = ()) -> tuple[re.Pattern[str], ...]:
    """Built-in shapes plus configured extras; an invalid configured pattern is a startup error."""

    patterns: list[re.Pattern[str]] = []
    for raw in (*BUILTIN_MANUAL_ONLY_SKU_PATTERNS, *extra):
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("Manual-only SKU patterns must be non-empty regular expressions.")
        patterns.append(re.compile(raw, re.IGNORECASE))
    return tuple(patterns)


def _description_sku(description: str | None) -> str | None:
    if not description:
        return None
    match = DESCRIPTION_SKU_TOKEN.search(description)
    return match.group(0) if match else None


def vat_key(value: str | None) -> str | None:
    """Comparison key for a VAT / VKN: alphanumerics only, upper-case, without a TR prefix."""

    if not value:
        return None
    compact = "".join(char for char in value if char.isalnum()).upper()
    if compact.startswith("TR"):
        compact = compact[2:]
    return compact or None


def _unit_code(value: str) -> str:
    return value.strip().upper()


def code_key(value: str) -> str:
    """Comparison key for product codes: whitespace removed, case-folded."""

    return "".join(value.split()).casefold()


def _within(target: CategoryPurchaseAccountConfiguration, root: CategoryPurchaseAccountConfiguration) -> bool:
    if target.category_id == root.category_id:
        return True
    target_path, root_path = target.category_complete_name, root.category_complete_name
    if not target_path or not root_path:
        # The subtree cannot be proven either way: fail closed.
        raise ProductLineCreatePolicyError(
            "Kategori hiyerarşisi okunamadı; kategori kısıtı doğrulanamadığı için ürün oluşturulmadı."
        )
    return target_path.startswith(f"{root_path} / ")


def _ids(ids: tuple[int, ...]) -> str:
    return ", ".join(f"#{value}" for value in ids[:5])


__all__ = [
    "BUILTIN_MANUAL_ONLY_SKU_PATTERNS",
    "ProductCatalogReader",
    "ProductLineCreationPolicy",
    "ValidatedNewProduct",
    "SupplierPartnerLookup",
    "code_key",
    "vat_key",
    "compile_manual_only_sku_patterns",
]
