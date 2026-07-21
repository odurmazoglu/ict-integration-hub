from __future__ import annotations

from typing import Protocol

from app.erp.models import Company


class CompanyRepository(Protocol):
    def find_by_id(self, company_id: int) -> Company | None:
        pass

    def find_default(self) -> Company | None:
        pass
