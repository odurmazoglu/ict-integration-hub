"""Explicit CREATE_NEW_PRODUCT remediation orchestration for a PRODUCT_NOT_FOUND review line (P0-PROD-07G).

Reuses the PR #140 narrow Odoo write infrastructure (``OdooProductWriter``,
``OdooSupplierInfoWriter``, ``OdooProductWritePolicy``, gated by
``PRODUCT_REMEDIATION_WRITE_ENABLED``) without redesigning it. This module carries
the crash-safe, retry-safe, concurrency-safe orchestration on top of it -- see
``product_remediation_use_cases.CreateNewProductUseCase`` for the state machine.

Two independent DB-enforced identities protect the two invariants:

* review-line ownership: ``(review_id, company_id, review_version, line_number)``
  -- the same review line can never independently execute CREATE_NEW_PRODUCT twice.
* supplier-product identity: ``(company_id, resolved_supplier_partner_id,
  normalized seller_item_code)`` -- two different reviews for the same supplier
  item can never race into two Odoo products.

Immutable source identity (``seller_item_code``, the resolved supplier
``partner_id``) is never trusted from the operator -- it is derived from the
review's persisted source evidence and its accepted ``SupplierResolution``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import ProductRemediationContractError

# Odoo v19 product.template.type choices exposed to the operator for CREATE_NEW_PRODUCT
# v1. A strict subset of ALLOWED_PRODUCT_TEMPLATE_TYPES in
# app.application.commands.product_remediation (#140's underlying writer also accepts
# "combo") -- "combo" is not yet a supported remediation outcome and must never be
# guessed/defaulted here; the operator explicitly picks goods vs. service.
ALLOWED_REMEDIATION_PRODUCT_TYPES = frozenset({"consu", "service"})


class ProductReservationStatus(StrEnum):
    """Internal durable state machine for one review-line's CREATE_NEW_PRODUCT reservation.

    Persisted BEFORE any Odoo write and advanced strictly forward. A resume always
    reads this status first -- it is the single source of truth for what remote
    work has (or may have) already happened.
    """

    #: Reservation committed; the owner has not yet attempted the Odoo product.template call.
    RESERVED = "reserved"
    #: The Odoo product.template create call is about to be (or was) attempted; the
    #: remote outcome is UNKNOWN until PRODUCT_CREATED is persisted. A resume seeing
    #: this status must never blindly retry the create -- see CreateNewProductUseCase.
    CREATE_ATTEMPTED = "create_attempted"
    #: product.template + product.product identity durably persisted. Odoo definitely
    #: has this product; product.supplierinfo has not been confirmed yet.
    PRODUCT_CREATED = "product_created"
    #: product.supplierinfo also confirmed (created or pre-existing). Terminal success.
    COMPLETED = "completed"
    #: This review line lost the supplier-product identity claim to another
    #: review/line whose product (and supplierinfo) is already resolved; that
    #: existing product was reused. Terminal success, zero Odoo writes by us.
    REUSED_EXISTING_PRODUCT = "reused_existing_product"
    #: The Odoo product.template outcome from a CREATE_ATTEMPTED state could not be
    #: deterministically proven either way. Terminal; requires human reconciliation.
    NEEDS_RECONCILIATION = "needs_reconciliation"


class ProductRemediationStatus(StrEnum):
    """Outcome of a :class:`CreateNewProductUseCase` request, returned to the caller."""

    #: The full flow completed: product resolved (created or reused) and supplierinfo resolved.
    COMPLETED = "completed"
    #: The Odoo product.template outcome from an earlier attempt is unprovable; a human
    #: must reconcile before this review line can be retried.
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True, slots=True)
class CreateNewProductCommand(ApplicationDTO):
    """An authenticated operator's explicit choice to create a new Odoo product for one review line.

    Only ``product_name``/``product_type``/``is_storable``/``internal_reference``/``note``
    are operator input carried into the Odoo write. Immutable source identity
    (``seller_item_code``, company, the resolved supplier partner) is never carried here
    -- it is derived from the review's persisted source evidence and accepted
    ``SupplierResolution`` by the use case. ``approved_by`` is the authenticated actor
    supplied by the API security context, never a body field (mirrors
    ``ResolveWorkbenchSupplierCommand``).

    ``product_type`` has no default: the operator must explicitly choose goods
    (``"consu"``) vs. service (``"service"``) -- it is never inferred from
    ``product_name``, ``seller_item_code``, the supplier, the invoice description, or
    ``uom_id`` (see PR fixing the P0-PROD-07F product-standard audit finding).

    ``authorization_id`` (P0-PROD-09G) is an optional reference to a previously
    issued, narrow, single-use write authorization (see
    ``app.application.workbench.write_authorization``) that lets
    ``CreateNewProductUseCase`` bypass the global ``PRODUCT_REMEDIATION_WRITE_ENABLED``
    gate for exactly this one write, without opening it globally. Never a blanket
    or wildcard grant; the master production kill switch, approval acknowledgement,
    and named-approver checks are never bypassed.

    ``categ_id`` (P0-PROD-18E-2) is the operator's explicit Odoo ``product.category``
    id -- an exact id, never a name/code, and never inferred from the product name,
    supplier, SKU, or category hierarchy. Optional in general; required (and
    restricted to ``RESALE_PRODUCT_CATEGORY_IDS``) when the review's current-version
    purchase purpose is RESALE. Validated read-only against Odoo before any write
    (see ``product_remediation_category``).
    """

    review_id: str
    company_id: int
    expected_version: int
    line_number: str
    product_name: str
    product_type: str
    uom_id: int
    approved_by: str
    is_storable: bool = False
    internal_reference: str | None = None
    note: str | None = None
    idempotency_key: str | None = None
    authorization_id: str | None = None
    categ_id: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.expected_version, "expected_version must be positive.")
        _require_text(self.line_number, "line_number is required.")
        _require_text(self.product_name, "product_name is required.")
        if self.product_type not in ALLOWED_REMEDIATION_PRODUCT_TYPES:
            raise ProductRemediationContractError(
                "product_type must be explicitly one of: " + ", ".join(sorted(ALLOWED_REMEDIATION_PRODUCT_TYPES))
            )
        _require_positive_int(self.uom_id, "A positive uom_id is required.")
        _require_text(self.approved_by, "approved_by (authenticated actor) is required.")
        if type(self.is_storable) is not bool:
            raise ProductRemediationContractError("is_storable must be boolean.")
        if self.internal_reference is not None and (
            not isinstance(self.internal_reference, str) or not self.internal_reference.strip()
        ):
            raise ProductRemediationContractError("internal_reference must be non-empty text when provided.")
        if self.note is not None and (not isinstance(self.note, str) or not self.note.strip()):
            raise ProductRemediationContractError("note must be non-empty text when provided.")
        if self.idempotency_key is not None and (
            not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip()
        ):
            raise ProductRemediationContractError("idempotency_key must be non-empty text when provided.")
        if self.authorization_id is not None and (
            not isinstance(self.authorization_id, str) or not self.authorization_id.strip()
        ):
            raise ProductRemediationContractError("authorization_id must be non-empty text when provided.")
        if self.categ_id is not None:
            _require_positive_int(self.categ_id, "categ_id must be a positive Odoo product.category id when provided.")


@dataclass(frozen=True, slots=True)
class ProductRemediationReservation(ApplicationDTO):
    """The persisted review-line ownership record for one CREATE_NEW_PRODUCT request.

    Immutable identity fields are set once at reservation time; ``status`` and the
    Odoo identity fields advance strictly forward as the orchestration progresses.
    ``categ_id`` (P0-PROD-18E-2) is part of that immutable intent: ``NULL`` for rows
    reserved before category support existed or without an explicit category, and a
    retry/recovery always uses the reserved value, never the caller's.
    """

    review_id: str
    company_id: int
    review_version: int
    line_number: str
    status: ProductReservationStatus
    resolved_supplier_partner_id: int
    seller_item_code: str
    product_name: str
    is_storable: bool
    internal_reference: str | None = None
    approved_by: str | None = None
    note: str | None = None
    idempotency_key: str | None = None
    product_template_id: int | None = None
    product_id: int | None = None
    supplierinfo_id: int | None = None
    categ_id: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.review_version, "review_version must be positive.")
        _require_text(self.line_number, "line_number is required.")
        if not isinstance(self.status, ProductReservationStatus):
            raise ProductRemediationContractError("A canonical ProductReservationStatus is required.")
        _require_positive_int(self.resolved_supplier_partner_id, "resolved_supplier_partner_id must be positive.")
        _require_text(self.seller_item_code, "seller_item_code is required.")
        _require_text(self.product_name, "product_name is required.")
        if type(self.is_storable) is not bool:
            raise ProductRemediationContractError("is_storable must be boolean.")
        for value, label in (
            (self.product_template_id, "product_template_id"),
            (self.product_id, "product_id"),
            (self.supplierinfo_id, "supplierinfo_id"),
            (self.categ_id, "categ_id"),
        ):
            if value is not None:
                _require_positive_int(value, f"{label} must be positive when set.")


@dataclass(frozen=True, slots=True)
class CreateNewProductResult(ApplicationDTO):
    """Typed, stable result of :class:`CreateNewProductUseCase`."""

    review_id: str
    company_id: int
    review_version: int
    line_number: str
    status: ProductRemediationStatus
    product_template_id: int | None = None
    product_id: int | None = None
    supplierinfo_id: int | None = None
    created_product: bool = False
    created_supplierinfo: bool = False
    reused_existing_product: bool = False
    already_applied: bool = False
    safe_message: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.review_version, "review_version must be positive.")
        _require_text(self.line_number, "line_number is required.")
        if not isinstance(self.status, ProductRemediationStatus):
            raise ProductRemediationContractError("A canonical ProductRemediationStatus is required.")
        for value, label in (
            (self.product_template_id, "product_template_id"),
            (self.product_id, "product_id"),
            (self.supplierinfo_id, "supplierinfo_id"),
        ):
            if value is not None:
                _require_positive_int(value, f"{label} must be positive when set.")


@dataclass(frozen=True, slots=True)
class ProductIdentityClaim(ApplicationDTO):
    """The persisted cross-review lock for one supplier-product identity.

    Purely a uniqueness lock + owner pointer -- the product/supplierinfo identity
    itself always lives on the owner's :class:`ProductRemediationReservation` row
    (single source of truth), reached via the owner fields here.
    """

    company_id: int
    resolved_supplier_partner_id: int
    seller_item_code: str
    owner_review_id: str
    owner_company_id: int
    owner_review_version: int
    owner_line_number: str

    def __post_init__(self) -> None:
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.resolved_supplier_partner_id, "resolved_supplier_partner_id must be positive.")
        _require_text(self.seller_item_code, "seller_item_code is required.")
        _require_text(self.owner_review_id, "owner_review_id is required.")
        _require_positive_int(self.owner_company_id, "owner_company_id must be positive.")
        _require_positive_int(self.owner_review_version, "owner_review_version must be positive.")
        _require_text(self.owner_line_number, "owner_line_number is required.")


@dataclass(frozen=True, slots=True)
class ExistingSupplierInfo(ApplicationDTO):
    """Minimal read-only projection of an existing Odoo ``product.supplierinfo``.

    Used only for the read-before-write natural-identity pre-check (STEP 5) --
    never as a substitute for the DB-enforced identity claim.
    """

    id: int
    partner_id: int | None
    product_tmpl_id: int | None
    product_id: int | None
    product_code: str | None
    company_id: int | None


def normalize_seller_item_code(value: str | None) -> str | None:
    """Whitespace-strip only -- identical to ``ProductMatchingEngine``'s own ``_clean``.

    Conservative and deterministic on purpose: no case-folding, no punctuation
    stripping, no approximate/near-match normalization. The supplier-product identity claim and the
    Odoo ``product.supplierinfo.product_code`` must agree on exactly the same
    normalization the deterministic matcher already uses, or a later re-import of
    the same invoice could fail to recognize the remediated product.
    """

    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise ProductRemediationContractError(message)


def _require_positive_int(value: int | None, message: str) -> None:
    if type(value) is not int or isinstance(value, bool) or value <= 0:
        raise ProductRemediationContractError(message)


__all__ = [
    "ALLOWED_REMEDIATION_PRODUCT_TYPES",
    "CreateNewProductCommand",
    "CreateNewProductResult",
    "ExistingSupplierInfo",
    "ProductIdentityClaim",
    "ProductReservationStatus",
    "ProductRemediationReservation",
    "ProductRemediationStatus",
    "normalize_seller_item_code",
]
