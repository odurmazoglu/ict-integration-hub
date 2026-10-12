"""PR D: fail-closed product master-data policy for Workbench new-product creation.

Decides only; every check is read-only and runs before any Odoo write. Grounded in the
ICT Product Master Data Policy draft (2026-09-16 production audit):

* **Supplier code is never Internal Reference** (§4/§5/§18). The seller code goes to
  ``product.supplierinfo.product_code`` only. An Internal Reference equal to it (ignoring
  case and whitespace) is refused, so a mechanical copy cannot slip through.
* **Variant/license catalogs are manual-only** (§12/§14/§17). ManageEngine license SKUs
  (``85710.1SS3``, ``67005.6MPMFA4``, ``702012.0SUP6`` ...) live on product *variants*
  with License Model / Pack / Users attributes; a single-variant product created here
  would be wrong master data. Detection is structural, never a vendor-name allowlist:
  a built-in SKU shape plus configured extra shapes, configured manual-only category
  subtrees (e.g. "Software Licenses / ManageEngine"), and any existing Odoo variant
  whose Internal Reference already equals the seller code.
* **Category, type and UoM are operator choices** (§6/§7/§8). Category is required and
  validated by the existing CREATE_NEW_PRODUCT category policy (RESALE: exact allowlist,
  non-storable, valid purchase account). UoM must be in ``PRODUCT_CREATION_UOM_IDS``.
* **No duplicates.** An active product with the same name, or any product already
  carrying the requested Internal Reference, is refused -- map the existing product
  instead ("Eşleştir").
"""

from __future__ import annotations

import re
from collections.abc import Iterable
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

#: Structural shapes of vendor license SKUs that belong to multi-variant catalogs.
#: Always active; ``PRODUCT_CREATION_MANUAL_ONLY_SKU_PATTERNS`` can only add to them.
BUILTIN_MANUAL_ONLY_SKU_PATTERNS: tuple[str, ...] = (
    # ManageEngine license SKUs: <4-6 digits>.<digit><2-8 alphanumerics>.
    r"^\d{4,6}\.\d[0-9A-Z]{2,8}$",
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
    ) -> None:
        self._category_policy = category_policy
        self._category_lister = category_lister
        self._catalog = catalog_reader
        self._allowed_uom_ids = frozenset(allowed_uom_ids)
        self._manual_only_category_ids = frozenset(manual_only_category_ids)
        self._sku_patterns = compile_manual_only_sku_patterns(extra_manual_only_sku_patterns)

    def validate(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
        seller_item_code: str,
        intent: NewProductIntent,
    ) -> ValidatedNewProduct:
        self.require_codes_allowed(seller_item_code=seller_item_code, intent=intent)
        if intent.uom_id not in self._allowed_uom_ids:
            raise ProductLineCreatePolicyError(
                "Seçilen ölçü birimi yeni ürünler için onaylı değil (PRODUCT_CREATION_UOM_IDS). "
                "ICT standardı 'Adet' (Units) birimidir."
            )
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

    def require_codes_allowed(self, *, seller_item_code: str, intent: NewProductIntent) -> None:
        if self._is_manual_only_sku(seller_item_code):
            raise ProductLineCreateManualOnlyError(MANUAL_ONLY_SKU_MESSAGE.format(code=seller_item_code))
        reference = intent.internal_reference
        if reference is None:
            return
        if code_key(reference) == code_key(seller_item_code):
            raise ProductLineCreatePolicyError(REFERENCE_IS_SUPPLIER_CODE_MESSAGE.format(code=seller_item_code))
        if self._is_manual_only_sku(reference):
            raise ProductLineCreateManualOnlyError(MANUAL_ONLY_REFERENCE_MESSAGE.format(code=reference))

    def _is_manual_only_sku(self, code: str) -> bool:
        compact = "".join(code.split())
        return any(pattern.fullmatch(compact) for pattern in self._sku_patterns)

    # ------------------------------------------------------------------ Odoo reads

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
    "code_key",
    "compile_manual_only_sku_patterns",
]
