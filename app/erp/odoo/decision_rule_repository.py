from __future__ import annotations

from typing import Any

from app.application.ports import DecisionRuleRepository
from app.application.rules import (
    InvoiceDecisionRule,
    OdooDecisionRuleAuthoringContractError,
    OdooDecisionRuleFieldMapping,
    order_invoice_decision_rules,
    validate_unique_odoo_decision_rule_identities,
)
from app.application.workbench import CurrencyReferenceRepository
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.decision_rule_mapper import (
    SAFE_DECISION_RULE_DATA_ERROR,
    OdooDecisionRuleDataError,
    OdooDecisionRuleMapper,
)

SAFE_DECISION_RULE_READ_ERROR = "Odoo Decision Rule read failed."


class OdooDecisionRuleReadError(ErpRepositoryError):
    pass


class OdooDecisionRuleRepository(DecisionRuleRepository):
    """Read Odoo-authored IPP Decision Rules into canonical immutable contracts."""

    def __init__(
        self,
        *,
        adapter: OdooReadOnlyAdapter,
        mapping: OdooDecisionRuleFieldMapping,
        currency_repository: CurrencyReferenceRepository,
        mapper: OdooDecisionRuleMapper | None = None,
    ) -> None:
        self._adapter = adapter
        self._mapping = mapping
        self._mapper = mapper or OdooDecisionRuleMapper(
            mapping=mapping,
            currency_repository=currency_repository,
        )

    def list_invoice_decision_rules(self, *, company_id: int) -> tuple[InvoiceDecisionRule, ...]:
        if type(company_id) is not int or company_id <= 0:
            raise OdooDecisionRuleDataError("company_id must be a positive integer.")
        try:
            records = self._adapter.search_read_all(
                model=self._mapping.model_name,
                domain=self._domain(company_id=company_id),
                fields=self._fields(),
            )
            rules = self._mapper.map_records(records, company_id=company_id)
            validate_unique_odoo_decision_rule_identities(rules)
            return order_invoice_decision_rules(rules)
        except OdooDecisionRuleDataError:
            raise
        except OdooDecisionRuleAuthoringContractError as exc:
            raise OdooDecisionRuleDataError(SAFE_DECISION_RULE_DATA_ERROR) from exc
        except ErpRepositoryError as exc:
            raise OdooDecisionRuleReadError(SAFE_DECISION_RULE_READ_ERROR) from exc
        except (TypeError, ValueError) as exc:
            raise OdooDecisionRuleDataError(SAFE_DECISION_RULE_DATA_ERROR) from exc

    def _domain(self, *, company_id: int) -> list[Any]:
        return [
            "&",
            [self._mapping.active, "=", True],
            "|",
            [self._mapping.company, "=", company_id],
            [self._mapping.company, "=", False],
        ]

    def _fields(self) -> list[str]:
        return _unique_fields("id", *self._mapping.studio_fields())


def _unique_fields(*fields: str) -> list[str]:
    unique: list[str] = []
    for field in fields:
        if field not in unique:
            unique.append(field)
    return unique
