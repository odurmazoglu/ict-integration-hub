from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.erp.repositories import (
    CompanyRepository,
    CurrencyRepository,
    PartnerRepository,
    ProductRepository,
    TaxRepository,
)


class RepositoryProvider(Protocol):
    @property
    def partner_repository(self) -> PartnerRepository:
        pass

    @property
    def product_repository(self) -> ProductRepository:
        pass

    @property
    def tax_repository(self) -> TaxRepository:
        pass

    @property
    def currency_repository(self) -> CurrencyRepository:
        pass

    @property
    def company_repository(self) -> CompanyRepository:
        pass


@dataclass(frozen=True, slots=True)
class StaticRepositoryProvider:
    partner_repository: PartnerRepository
    product_repository: ProductRepository
    tax_repository: TaxRepository
    currency_repository: CurrencyRepository
    company_repository: CompanyRepository
