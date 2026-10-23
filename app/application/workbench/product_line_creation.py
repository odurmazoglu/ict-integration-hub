"""PR D: create a NEW Odoo product for one PRODUCT_NOT_FOUND Workbench line.

The operator fills a small typed intent on a Workbench child product line row -- product
name, kind (service / non-stock goods / stock goods), Odoo category, Odoo unit of measure
and an optional Internal Reference -- and presses "Yeni Ürün Oluştur". The Hub creates
exactly one simple ``product.template`` whose supplier mapping
``(effective supplier, seller product code) -> product`` is created *in the same Odoo
transaction* (one inline ``seller_ids`` row), then reclassifies the review
(``MASTER_DATA_CHANGED``), so the line matches now and every future invoice of the same
supplier and code matches automatically.

Never taken from the operator: the seller product code (immutable source line), the
supplier partner (the review's effective supplier), and the line identity. Never
derived: product type, category, unit of measure, Internal Reference -- each is an
explicit operator choice, and the supplier's own code is never copied into
``default_code`` (see ``product_line_creation_policy``).

Crash safety. ``product.template`` has no Hub idempotency key, but the atomic supplier
mapping is one: it exists iff the create committed. The Hub records
``CREATE_ATTEMPTED`` (with the identity claim and the consumed authorization) before
the Odoo call; a resume finds the mapping and adopts the product, or -- only after the
in-flight grace -- proves its absence and creates afresh. Anything that does not match
the recorded intent exactly is ``NEEDS_RECONCILIATION`` (a human decides), never a guess.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import (
    ProductRemediationConflictError,
    ProductRemediationContractError,
    ProductRemediationDataIntegrityError,
    ProductRemediationEligibilityError,
)
from app.application.workbench.product_remediation import CLAIM_OWNER_PRODUCT_LINE_CREATION, ProductIdentityClaim

#: Bounds an operator-typed product name; product.template names are short in practice.
MAX_PRODUCT_NAME_LENGTH = 255
#: Bounds an operator-typed Internal Reference (ICT convention: short codes like ME-SI-U).
MAX_INTERNAL_REFERENCE_LENGTH = 64


class NewProductKind(StrEnum):
    """The operator's single explicit choice of product type *and* storability.

    One selection instead of a type plus a boolean: a boolean cannot distinguish
    "not storable" from "not answered", and storability is an operator judgement for
    goods (ICT Product Master Data Policy §6/§9).
    """

    SERVICE = "service"
    GOODS = "goods"
    STOCKED_GOODS = "stocked_goods"

    @property
    def product_type(self) -> str:
        return "service" if self is NewProductKind.SERVICE else "consu"

    @property
    def is_storable(self) -> bool:
        return self is NewProductKind.STOCKED_GOODS


@dataclass(frozen=True, slots=True)
class NewProductIntent(ApplicationDTO):
    """Immutable operator intent for one new product; every field is an explicit choice."""

    product_name: str
    kind: NewProductKind
    categ_id: int
    uom_id: int
    internal_reference: str | None = None
    #: V1: whether the new product may be sold (``product.template.sale_ok``). An explicit
    #: operator choice read from the Workbench row -- unchecked means ``False`` -- never
    #: inferred from the purchase purpose, the supplier or the category. Products created
    #: here are always purchasable (``purchase_ok=True``). ``None`` only for a creation
    #: recorded before this field existed (Odoo defaults applied then); a new creation
    #: always carries a boolean.
    sale_ok: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.product_name, str) or not self.product_name.strip():
            raise ProductRemediationContractError("Yeni ürün adı girilmelidir.")
        name = self.product_name.strip()
        if len(name) > MAX_PRODUCT_NAME_LENGTH or any(ord(char) < 32 for char in name):
            raise ProductRemediationContractError(
                f"Yeni ürün adı tek satır ve en fazla {MAX_PRODUCT_NAME_LENGTH} karakter olmalıdır."
            )
        object.__setattr__(self, "product_name", name)
        if not isinstance(self.kind, NewProductKind):
            raise ProductRemediationContractError("Ürün türü seçilmelidir.")
        for value, label in ((self.categ_id, "Ürün kategorisi"), (self.uom_id, "Ölçü birimi")):
            if type(value) is not int or value <= 0:
                raise ProductRemediationContractError(f"{label} seçilmelidir.")
        if self.internal_reference is not None:
            if not isinstance(self.internal_reference, str):
                raise ProductRemediationContractError("İç referans metin olmalıdır.")
            reference = self.internal_reference.strip()
            if len(reference) > MAX_INTERNAL_REFERENCE_LENGTH or any(ord(char) < 32 for char in reference):
                raise ProductRemediationContractError(
                    f"İç referans tek satır ve en fazla {MAX_INTERNAL_REFERENCE_LENGTH} karakter olmalıdır."
                )
            object.__setattr__(self, "internal_reference", reference or None)
        if self.sale_ok is not None and not isinstance(self.sale_ok, bool):
            raise ProductRemediationContractError("Satılabilir seçimi evet/hayır olmalıdır.")

    @property
    def product_type(self) -> str:
        return self.kind.product_type

    @property
    def is_storable(self) -> bool:
        return self.kind.is_storable

    def digest(self) -> str:
        """Stable fingerprint of the whole intent (authorization binding, audit)."""

        payload = {
            "product_name": self.product_name,
            "kind": self.kind.value,
            "categ_id": self.categ_id,
            "uom_id": self.uom_id,
            "internal_reference": self.internal_reference,
        }
        if self.sale_ok is not None:
            # Absent for a pre-V1 creation, so recorded digests (and the authorizations bound
            # to them) stay valid; always present for a new one.
            payload["sale_ok"] = self.sale_ok
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CreateLineProductCommand(ApplicationDTO):
    review_id: str
    company_id: int
    expected_version: int
    line_number: str
    intent: NewProductIntent
    approved_by: str
    authorization_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.review_id, str) or not self.review_id.strip():
            raise ProductRemediationContractError("review_id is required.")
        for value, label in ((self.company_id, "company_id"), (self.expected_version, "expected_version")):
            if type(value) is not int or value <= 0:
                raise ProductRemediationContractError(f"{label} must be positive.")
        if not isinstance(self.line_number, str) or not self.line_number.strip():
            raise ProductRemediationContractError("Fatura satırı seçilmelidir.")
        if not isinstance(self.intent, NewProductIntent):
            raise ProductRemediationContractError("A canonical NewProductIntent is required.")
        if not isinstance(self.approved_by, str) or not self.approved_by.strip():
            raise ProductRemediationContractError("approved_by (authenticated actor) is required.")
        if self.authorization_id is not None and (
            not isinstance(self.authorization_id, str) or not self.authorization_id.strip()
        ):
            raise ProductRemediationContractError("authorization_id must be non-empty text when provided.")


class ProductLineCreationStatus(StrEnum):
    """Durable state of one line's creation; advanced strictly forward."""

    #: Identity claimed, authorization consumed, Odoo create about to run (or ran):
    #: the remote outcome is unknown until PRODUCT_CREATED is recorded.
    CREATE_ATTEMPTED = "create_attempted"
    #: Odoo template + variant + supplier mapping exist; ids recorded. Not reclassified yet.
    PRODUCT_CREATED = "product_created"
    #: Reclassified (MASTER_DATA_CHANGED) in the same transaction. Terminal success.
    COMPLETED = "completed"
    #: Odoo state does not match the recorded intent exactly. Terminal; a human decides.
    NEEDS_RECONCILIATION = "needs_reconciliation"


@dataclass(frozen=True, slots=True)
class ProductLineCreation(ApplicationDTO):
    """The persisted creation record for ``(review_id, company_id, review_version, line_number)``."""

    review_id: str
    company_id: int
    review_version: int
    line_number: str
    status: ProductLineCreationStatus
    supplier_partner_id: int
    seller_item_code: str
    intent: NewProductIntent
    approved_by: str
    create_attempted_at: datetime
    supplier_product_name: str | None = None
    product_template_id: int | None = None
    product_id: int | None = None
    supplierinfo_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ProductLineCreationStatus):
            raise ProductRemediationDataIntegrityError("A canonical ProductLineCreationStatus is required.")
        if not isinstance(self.intent, NewProductIntent):
            raise ProductRemediationDataIntegrityError("A canonical NewProductIntent is required.")
        for value in (self.company_id, self.review_version, self.supplier_partner_id):
            if type(value) is not int or value <= 0:
                raise ProductRemediationDataIntegrityError("Product line creation ids must be positive.")
        if not isinstance(self.create_attempted_at, datetime) or self.create_attempted_at.utcoffset() is None:
            raise ProductRemediationDataIntegrityError("create_attempted_at must be timezone-aware.")
        if self.status in (ProductLineCreationStatus.PRODUCT_CREATED, ProductLineCreationStatus.COMPLETED) and (
            self.product_template_id is None or self.product_id is None
        ):
            raise ProductRemediationDataIntegrityError("A created product must record its template and variant.")
        if self.status is ProductLineCreationStatus.COMPLETED and self.supplierinfo_id is None:
            raise ProductRemediationDataIntegrityError("A completed creation must record its supplier mapping.")

    def identity_claim(self) -> ProductIdentityClaim:
        return ProductIdentityClaim(
            company_id=self.company_id,
            resolved_supplier_partner_id=self.supplier_partner_id,
            seller_item_code=self.seller_item_code,
            owner_review_id=self.review_id,
            owner_company_id=self.company_id,
            owner_review_version=self.review_version,
            owner_line_number=self.line_number,
            owner_kind=CLAIM_OWNER_PRODUCT_LINE_CREATION,
        )


@dataclass(frozen=True, slots=True)
class CreateLineProductResult(ApplicationDTO):
    review_id: str
    company_id: int
    line_number: str
    seller_item_code: str
    supplier_partner_id: int
    product_name: str
    product_template_id: int
    product_id: int
    supplierinfo_id: int
    #: The product was created by this call (False for an adopted or already-applied one).
    created_product: bool
    #: The request had already completed; nothing was written again.
    already_applied: bool
    previous_version: int
    current_version: int
    #: The line no longer carries PRODUCT_NOT_FOUND after reclassification.
    line_resolved: bool
    remaining_product_lines: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- errors


class ProductLineCreatePolicyError(ProductRemediationEligibilityError):
    """The intent violates the product master-data policy; nothing was written."""

    error_category = "product_line_create_policy"


class ProductLineCreateManualOnlyError(ProductLineCreatePolicyError):
    """The SKU or category belongs to a variant/license catalog: manual Odoo creation only."""

    error_category = "product_line_create_manual_only"


class ProductLineCreateDuplicateError(ProductRemediationConflictError):
    """An equivalent product or supplier mapping already exists; nothing was created."""

    error_category = "product_line_create_duplicate"


class ProductLineCreateInFlightError(ProductRemediationConflictError):
    """An earlier attempt's Odoo outcome is still inside the in-flight grace; retry later."""

    error_category = "product_line_create_in_flight"


class ProductLineCreateOutcomeUnknownError(ProductRemediationConflictError):
    """The Odoo create was sent but its outcome is unknown; a later resubmission verifies it."""

    error_category = "product_line_create_outcome_unknown"


class ProductLineCreateReconciliationRequiredError(ProductRemediationDataIntegrityError):
    """Odoo state contradicts the recorded intent; terminal until a human reconciles."""

    error_category = "product_line_create_reconciliation_required"


# --------------------------------------------------------------------------- ports


class ProductLineCreationRepository(Protocol):
    def find(
        self, *, review_id: str, company_id: int, review_version: int, line_number: str
    ) -> ProductLineCreation | None: ...

    def insert_attempt(self, creation: ProductLineCreation) -> ProductLineCreation:
        """Insert a CREATE_ATTEMPTED row; a concurrent insert of the same line raises
        ``ProductRemediationRaceError``."""

    def advance(
        self,
        creation: ProductLineCreation,
        *,
        expected_status: ProductLineCreationStatus,
        new_status: ProductLineCreationStatus,
        product_template_id: int | None = None,
        product_id: int | None = None,
        supplierinfo_id: int | None = None,
    ) -> ProductLineCreation:
        """Compare-and-swap transition; refuses unless the row is still ``expected_status``."""

    def discard_attempt(self, creation: ProductLineCreation) -> None:
        """Delete the row iff it is still CREATE_ATTEMPTED (only when no Odoo write can exist)."""


class ProductIdentityClaimStore(Protocol):
    """The shared supplier-product identity lock (also used by REST CREATE_NEW_PRODUCT)."""

    def claim(self, claim: ProductIdentityClaim) -> ProductIdentityClaim: ...

    def find(
        self, *, company_id: int, resolved_supplier_partner_id: int, seller_item_code: str
    ) -> ProductIdentityClaim | None: ...

    def release(self, claim: ProductIdentityClaim) -> None:
        """Delete exactly this claim (same identity *and* owner); a no-op when absent."""


@dataclass(frozen=True, slots=True)
class CreatedProductSnapshot(ApplicationDTO):
    """Read-only Odoo view of a template, for adopting a product after a lost response."""

    template_id: int
    name: str | None
    default_code: str | None
    product_type: str | None
    uom_id: int | None
    categ_id: int | None
    is_storable: bool | None
    variant_ids: tuple[int, ...]
    sale_ok: bool | None = None
    purchase_ok: bool | None = None


class CreatedProductReader(Protocol):
    def read_template(self, template_id: int) -> CreatedProductSnapshot | None: ...


__all__ = [
    "MAX_INTERNAL_REFERENCE_LENGTH",
    "MAX_PRODUCT_NAME_LENGTH",
    "CreateLineProductCommand",
    "CreateLineProductResult",
    "CreatedProductReader",
    "CreatedProductSnapshot",
    "NewProductIntent",
    "NewProductKind",
    "ProductIdentityClaimStore",
    "ProductLineCreateDuplicateError",
    "ProductLineCreateInFlightError",
    "ProductLineCreateManualOnlyError",
    "ProductLineCreateOutcomeUnknownError",
    "ProductLineCreatePolicyError",
    "ProductLineCreateReconciliationRequiredError",
    "ProductLineCreation",
    "ProductLineCreationRepository",
    "ProductLineCreationStatus",
]
