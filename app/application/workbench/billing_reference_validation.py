from __future__ import annotations

from collections.abc import Callable, Iterable

from app.application.exceptions import ApplicationError
from app.application.workbench.billing_authoring import (
    ValidatedWorkbenchBillingAuthoring,
    WorkbenchBillingAuthoringRow,
)
from app.application.workbench.erp_references import (
    CurrencyReference,
    CurrencyReferenceRepository,
    PartnerReference,
    PartnerReferenceRepository,
    ProductReference,
    ProductReferenceRepository,
    SalesTaxReference,
    SalesTaxReferenceRepository,
)
from app.application.workbench.exceptions import (
    WorkbenchContractError,
    WorkbenchErpReferenceCompanyMismatchError,
    WorkbenchErpReferenceNotFoundError,
    WorkbenchErpReferenceTypeError,
    WorkbenchErpReferenceValidationError,
)

SAFE_BILLING_REFERENCE_FAILURE = "Billing ERP reference validation failed."


class WorkbenchBillingReferenceValidator:
    """Read-only exact validator for Odoo-authored Customer Invoice billing references."""

    def __init__(
        self,
        *,
        partner_repository: PartnerReferenceRepository,
        product_repository: ProductReferenceRepository,
        sales_tax_repository: SalesTaxReferenceRepository,
        currency_repository: CurrencyReferenceRepository,
    ) -> None:
        self._partner_repository = partner_repository
        self._product_repository = product_repository
        self._sales_tax_repository = sales_tax_repository
        self._currency_repository = currency_repository

    def validate_billing_authoring(
        self,
        rows: tuple[WorkbenchBillingAuthoringRow, ...],
        *,
        requested_company_id: int,
    ) -> ValidatedWorkbenchBillingAuthoring:
        if type(requested_company_id) is not int or requested_company_id <= 0:
            raise WorkbenchContractError("requested company_id must be positive.")
        for row in rows:
            if not isinstance(row, WorkbenchBillingAuthoringRow):
                raise WorkbenchContractError("WorkbenchBillingAuthoringRow values are required.")
        references = _translate_failure(lambda: self._read_references(rows))
        for row in rows:
            partner = _required_reference(
                row.customer_id,
                references.partners,
                "Billing customer reference is invalid.",
            )
            _validate_optional_company(partner.company_id, requested_company_id=requested_company_id)
            product = _required_reference(
                row.product_id,
                references.products,
                "Billing product reference is invalid.",
            )
            _validate_optional_company(product.company_id, requested_company_id=requested_company_id)
            if product.active is not True:
                raise WorkbenchErpReferenceTypeError("Billing product reference is inactive.")
            currency = _required_reference(
                row.currency_id,
                references.currencies,
                "Billing currency reference is invalid.",
            )
            if currency.active is not True:
                raise WorkbenchErpReferenceTypeError("Billing currency reference is inactive.")
            for tax_id in row.sales_tax_ids:
                tax = _required_reference(
                    tax_id,
                    references.sales_taxes,
                    "Billing sales tax reference is invalid.",
                )
                _validate_optional_company(tax.company_id, requested_company_id=requested_company_id)
                if tax.active is not True:
                    raise WorkbenchErpReferenceTypeError("Billing sales tax reference is inactive.")
                if tax.usage_type is not None and tax.usage_type != "sale":
                    raise WorkbenchErpReferenceTypeError("Billing sales tax reference is not an outgoing tax.")
        return ValidatedWorkbenchBillingAuthoring(
            rows=rows,
            currency_codes_by_id=tuple(
                (currency_id, references.currencies[currency_id].code)
                for currency_id in sorted({row.currency_id for row in rows})
            ),
        )

    def _read_references(self, rows: tuple[WorkbenchBillingAuthoringRow, ...]) -> _BillingReferenceMaps:
        return _BillingReferenceMaps(
            partners=_by_id(
                self._partner_repository.find_partners_by_ids(_unique_ints(row.customer_id for row in rows))
            ),
            products=_by_id(
                self._product_repository.find_products_by_ids(_unique_ints(row.product_id for row in rows))
            ),
            sales_taxes=_by_id(
                self._sales_tax_repository.find_sales_taxes_by_ids(
                    _unique_ints(tax_id for row in rows for tax_id in row.sales_tax_ids)
                )
            ),
            currencies=_by_id(
                self._currency_repository.find_currencies_by_ids(_unique_ints(row.currency_id for row in rows))
            ),
        )


class _BillingReferenceMaps:
    def __init__(
        self,
        *,
        partners: dict[int, PartnerReference],
        products: dict[int, ProductReference],
        sales_taxes: dict[int, SalesTaxReference],
        currencies: dict[int, CurrencyReference],
    ) -> None:
        self.partners = partners
        self.products = products
        self.sales_taxes = sales_taxes
        self.currencies = currencies


def _translate_failure[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    try:
        return operation()
    except ApplicationError:
        raise
    except Exception as exc:
        raise WorkbenchErpReferenceValidationError(SAFE_BILLING_REFERENCE_FAILURE) from exc


def _unique_ints(values: Iterable[int]) -> tuple[int, ...]:
    return tuple(sorted(set(values)))


def _by_id[ReferenceT](references: tuple[ReferenceT, ...]) -> dict[int, ReferenceT]:
    return {reference.id: reference for reference in references}


def _required_reference[KeyT, ReferenceT](
    key: KeyT,
    references: dict[KeyT, ReferenceT],
    message: str,
) -> ReferenceT:
    reference = references.get(key)
    if reference is None:
        raise WorkbenchErpReferenceNotFoundError(message)
    return reference


def _validate_optional_company(company_id: int | None, *, requested_company_id: int) -> None:
    if company_id is not None and company_id != requested_company_id:
        raise WorkbenchErpReferenceCompanyMismatchError("Billing ERP reference company scope mismatch.")
