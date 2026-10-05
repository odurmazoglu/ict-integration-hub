"""Read-side facts and the eligibility policy for CAPITALIZE_FIXED_ASSET selections.

The ERP adapter behind :class:`FixedAssetAccountingReader` only *reads* the selected
``account.account`` and ``account.depreciation.model``;
every eligibility rule lives here, in the application layer:

* asset account -- in the explicit ``ODOO_FIXED_ASSET_ACCOUNT_IDS`` allowlist (empty
  allowlist fails closed), exists, active, company-compatible, ``account_type =
  asset_fixed`` and ``can_create_asset`` true where this Odoo version exposes it. The
  allowlist is what keeps e.g. an *accumulated depreciation* account -- also
  ``asset_fixed`` in the Turkish chart -- from ever being selected.
* asset posting accounts -- since Odoo saas~19.2 the accumulated depreciation
  (``asset_depreciation_account_id``) and depreciation expense
  (``asset_expense_account_id``) accounts live on the asset account, not on the
  depreciation model. Where this Odoo version exposes them both must be configured,
  distinct from the asset account, active and company-compatible; otherwise the posted
  bill would yield an asset Odoo cannot depreciate.
* depreciation model -- exists, active, and either global (no company) or the review's
  company. No useful life / method is hard-coded: the operator chooses the model.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import (
    DepreciationModelInvalidError,
    FixedAssetAccountingUnavailableError,
    FixedAssetAccountInvalidError,
)

FIXED_ASSET_ACCOUNT_TYPE = "asset_fixed"


@dataclass(frozen=True, slots=True)
class FixedAssetAccountRecord(ApplicationDTO):
    """Read-only projection of one ``account.account``."""

    id: int
    code: str
    name: str
    account_type: str
    active: bool
    company_ids: tuple[int, ...] = field(default_factory=tuple)
    #: ``None`` when this Odoo version has no ``can_create_asset`` field.
    can_create_asset: bool | None = None
    #: ``False`` when this Odoo version has no account-level asset posting accounts
    #: (pre-saas~19.2, where asset models carried them); the ids are then ignored.
    asset_posting_accounts_supported: bool = False
    asset_depreciation_account_id: int | None = None
    asset_expense_account_id: int | None = None


@dataclass(frozen=True, slots=True)
class DepreciationModelRecord(ApplicationDTO):
    """Read-only projection of one ``account.depreciation.model``."""

    id: int
    name: str
    active: bool
    company_id: int | None = None
    method: str | None = None
    method_number: float | None = None
    method_period: str | None = None


class FixedAssetAccountingReader(Protocol):
    """Read-only port: fixed model, fixed fields, lookup by id only."""

    def read_account(self, *, account_id: int) -> FixedAssetAccountRecord | None: ...

    def read_depreciation_model(self, *, model_id: int) -> DepreciationModelRecord | None: ...


@dataclass(frozen=True, slots=True)
class FixedAssetAccountPolicy:
    """The deployment allowlist of selectable fixed-asset accounts (no production ids in code)."""

    allowed_account_ids: frozenset[int] = frozenset()

    @classmethod
    def from_ids(cls, ids: Iterable[int]) -> FixedAssetAccountPolicy:
        return cls(allowed_account_ids=frozenset(ids))

    def require_eligible_account(
        self,
        reader: FixedAssetAccountingReader | None,
        *,
        company_id: int,
        account_id: int,
    ) -> FixedAssetAccountRecord:
        if reader is None:
            raise FixedAssetAccountingUnavailableError("Fixed-asset accounting is not configured for this workflow.")
        if not self.allowed_account_ids:
            raise FixedAssetAccountingUnavailableError(
                "No fixed-asset accounts are approved (ODOO_FIXED_ASSET_ACCOUNT_IDS is empty)."
            )
        if account_id not in self.allowed_account_ids:
            raise FixedAssetAccountInvalidError("The selected asset account is not an approved fixed-asset account.")
        account = reader.read_account(account_id=account_id)
        if account is None or account.id != account_id:
            raise FixedAssetAccountInvalidError("The selected asset account does not exist.")
        if not account.active:
            raise FixedAssetAccountInvalidError("The selected asset account is not active.")
        if company_id not in account.company_ids:
            raise FixedAssetAccountInvalidError("The selected asset account is not scoped to this company.")
        if account.account_type != FIXED_ASSET_ACCOUNT_TYPE:
            raise FixedAssetAccountInvalidError("The selected asset account is not a fixed-asset account.")
        if account.can_create_asset is False:
            raise FixedAssetAccountInvalidError("The selected asset account cannot create assets in Odoo.")
        if account.asset_posting_accounts_supported:
            _require_asset_posting_account(
                reader,
                company_id=company_id,
                asset_account_id=account_id,
                posting_account_id=account.asset_depreciation_account_id,
                label="accumulated depreciation",
            )
            _require_asset_posting_account(
                reader,
                company_id=company_id,
                asset_account_id=account_id,
                posting_account_id=account.asset_expense_account_id,
                label="depreciation expense",
            )
        return account


def _require_asset_posting_account(
    reader: FixedAssetAccountingReader,
    *,
    company_id: int,
    asset_account_id: int,
    posting_account_id: int | None,
    label: str,
) -> None:
    if posting_account_id is None:
        raise FixedAssetAccountInvalidError(f"The selected asset account has no {label} account configured in Odoo.")
    if posting_account_id == asset_account_id:
        raise FixedAssetAccountInvalidError(f"The selected asset account uses itself as its {label} account in Odoo.")
    posting_account = reader.read_account(account_id=posting_account_id)
    if posting_account is None or posting_account.id != posting_account_id:
        raise FixedAssetAccountInvalidError(f"The {label} account of the selected asset account does not exist.")
    if not posting_account.active:
        raise FixedAssetAccountInvalidError(f"The {label} account of the selected asset account is not active.")
    if company_id not in posting_account.company_ids:
        raise FixedAssetAccountInvalidError(
            f"The {label} account of the selected asset account is not scoped to this company."
        )


def require_eligible_depreciation_model(
    reader: FixedAssetAccountingReader | None,
    *,
    company_id: int,
    model_id: int,
) -> DepreciationModelRecord:
    if reader is None:
        raise FixedAssetAccountingUnavailableError("Fixed-asset accounting is not configured for this workflow.")
    model = reader.read_depreciation_model(model_id=model_id)
    if model is None or model.id != model_id:
        raise DepreciationModelInvalidError("The selected depreciation model does not exist.")
    if not model.active:
        raise DepreciationModelInvalidError("The selected depreciation model is not active.")
    if model.company_id is not None and model.company_id != company_id:
        raise DepreciationModelInvalidError("The selected depreciation model belongs to a different company.")
    return model


__all__ = [
    "FIXED_ASSET_ACCOUNT_TYPE",
    "DepreciationModelRecord",
    "FixedAssetAccountPolicy",
    "FixedAssetAccountRecord",
    "FixedAssetAccountingReader",
    "require_eligible_depreciation_model",
]
