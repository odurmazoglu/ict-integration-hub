from __future__ import annotations

from typing import Any

from app.application.ports import DecisionRuleRepository
from app.application.rules import (
    InvoiceDecisionRule,
    OdooDecisionRuleAuthoringContractError,
    OdooDecisionRuleAuthoringRecord,
    OdooDecisionRuleFieldMapping,
    order_invoice_decision_rules,
    validate_unique_odoo_decision_rule_identities,
)
from app.application.workbench import CurrencyReferenceRepository
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.adapter import OdooReadOnlyAdapter, many2one_id

SAFE_DECISION_RULE_READ_ERROR = "Odoo Decision Rule read failed."
SAFE_DECISION_RULE_DATA_ERROR = "Odoo Decision Rule configuration is invalid."

PO_ANY = "any"
PO_REQUIRED = "required"
PO_MUST_NOT_EXIST = "must_not_exist"


class OdooDecisionRuleReadError(ErpRepositoryError):
    pass


class OdooDecisionRuleDataError(ErpRepositoryError):
    pass


class OdooDecisionRuleRepository(DecisionRuleRepository):
    """Read Odoo-authored IPP Decision Rules into canonical immutable contracts."""

    def __init__(
        self,
        *,
        adapter: OdooReadOnlyAdapter,
        mapping: OdooDecisionRuleFieldMapping,
        currency_repository: CurrencyReferenceRepository,
    ) -> None:
        self._adapter = adapter
        self._mapping = mapping
        self._currency_repository = currency_repository

    def list_invoice_decision_rules(self, *, company_id: int) -> tuple[InvoiceDecisionRule, ...]:
        if type(company_id) is not int or company_id <= 0:
            raise OdooDecisionRuleDataError("company_id must be a positive integer.")
        try:
            records = self._adapter.search_read_all(
                model=self._mapping.model_name,
                domain=self._domain(company_id=company_id),
                fields=self._fields(),
            )
            currency_codes = self._currency_codes(records, company_id=company_id)
            rules = tuple(
                self._rule_from_record(record, company_id=company_id, currency_codes=currency_codes)
                for record in records
                if self._is_applicable_active_record(record, company_id=company_id)
            )
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

    def _currency_codes(self, records: tuple[dict[str, Any], ...], *, company_id: int) -> dict[int, str]:
        currency_ids = tuple(
            sorted(
                {
                    currency_id
                    for record in records
                    if self._is_applicable_active_record(record, company_id=company_id)
                    if (currency_id := _optional_many2one_id(record.get(self._mapping.currency))) is not None
                }
            )
        )
        if not currency_ids:
            return {}
        references = self._currency_repository.find_currencies_by_ids(currency_ids)
        active_by_id = {reference.id: reference for reference in references if reference.active}
        missing = tuple(currency_id for currency_id in currency_ids if currency_id not in active_by_id)
        if missing:
            raise OdooDecisionRuleDataError(SAFE_DECISION_RULE_DATA_ERROR)
        return {reference.id: reference.code for reference in active_by_id.values()}

    def _is_applicable_active_record(self, record: dict[str, Any], *, company_id: int | None) -> bool:
        active = _required_bool(record.get(self._mapping.active))
        if active is False:
            return False
        record_company_id = _optional_many2one_id(record.get(self._mapping.company))
        if company_id is None:
            return True
        return record_company_id in {None, company_id}

    def _rule_from_record(
        self,
        record: dict[str, Any],
        *,
        company_id: int,
        currency_codes: dict[int, str],
    ) -> InvoiceDecisionRule:
        record_company_id = _optional_many2one_id(record.get(self._mapping.company))
        if record_company_id not in {None, company_id}:
            raise OdooDecisionRuleDataError(SAFE_DECISION_RULE_DATA_ERROR)
        currency_id = _optional_many2one_id(record.get(self._mapping.currency))
        return OdooDecisionRuleAuthoringRecord(
            odoo_record_id=_required_positive_int(record.get("id")),
            name=_required_text(record.get(self._mapping.name)),
            rule_code=_required_text(record.get(self._mapping.rule_code)),
            active=_required_bool(record.get(self._mapping.active)),
            priority=_required_non_negative_int(record.get(self._mapping.priority)),
            rule_version=_required_positive_int(record.get(self._mapping.rule_version)),
            workflow=_required_text(record.get(self._mapping.workflow)),
            classification_code=_optional_text(record.get(self._mapping.classification_code)),
            company_id=record_company_id,
            vendor_partner_id=_optional_many2one_id(record.get(self._mapping.vendor)),
            vendor_tax_id=_optional_text(record.get(self._mapping.vendor_tax_id)),
            currency_id=currency_id,
            currency_code=currency_codes.get(currency_id) if currency_id is not None else None,
            provider_document_type=_optional_text(record.get(self._mapping.provider_document_type)),
            purchase_order_present=_purchase_order_present(record.get(self._mapping.purchase_order_present)),
            description_contains=_description_terms(record.get(self._mapping.description_contains)),
            product_mapping_id=_optional_many2one_id(record.get(self._mapping.product_mapping)),
            require_review=_required_bool(record.get(self._mapping.require_review)),
            require_business_context=_required_bool(record.get(self._mapping.require_business_context)),
            notes=_optional_text(record.get(self._mapping.notes)),
        ).to_invoice_decision_rule()


def _required_bool(value: Any) -> bool:
    if type(value) is not bool:
        raise OdooDecisionRuleDataError(SAFE_DECISION_RULE_DATA_ERROR)
    return value


def _required_positive_int(value: Any) -> int:
    parsed = _exact_int(value)
    if parsed <= 0:
        raise OdooDecisionRuleDataError(SAFE_DECISION_RULE_DATA_ERROR)
    return parsed


def _required_non_negative_int(value: Any) -> int:
    parsed = _exact_int(value)
    if parsed < 0:
        raise OdooDecisionRuleDataError(SAFE_DECISION_RULE_DATA_ERROR)
    return parsed


def _exact_int(value: Any) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise OdooDecisionRuleDataError(SAFE_DECISION_RULE_DATA_ERROR)


def _optional_many2one_id(value: Any) -> int | None:
    if value in (None, False):
        return None
    reference_id = many2one_id(value)
    if reference_id is None or reference_id <= 0:
        raise OdooDecisionRuleDataError(SAFE_DECISION_RULE_DATA_ERROR)
    return reference_id


def _required_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OdooDecisionRuleDataError(SAFE_DECISION_RULE_DATA_ERROR)
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value in (None, False):
        return None
    return _required_text(value)


def _purchase_order_present(value: Any) -> bool | None:
    if value in (None, False, PO_ANY):
        return None
    if value == PO_REQUIRED:
        return True
    if value == PO_MUST_NOT_EXIST:
        return False
    raise OdooDecisionRuleDataError(SAFE_DECISION_RULE_DATA_ERROR)


def _description_terms(value: Any) -> tuple[str, ...]:
    if value in (None, False):
        return ()
    if not isinstance(value, str):
        raise OdooDecisionRuleDataError(SAFE_DECISION_RULE_DATA_ERROR)
    return tuple(line.strip() for line in value.splitlines() if line.strip())


def _unique_fields(*fields: str) -> list[str]:
    unique: list[str] = []
    for field in fields:
        if field not in unique:
            unique.append(field)
    return unique
