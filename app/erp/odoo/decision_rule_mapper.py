from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.application.rules import (
    InvoiceDecisionRule,
    OdooDecisionRuleAuthoringRecord,
    OdooDecisionRuleFieldMapping,
)
from app.application.workbench import CurrencyReferenceRepository
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.adapter import many2one_id

SAFE_DECISION_RULE_DATA_ERROR = "Odoo Decision Rule configuration is invalid."

PO_ANY = "any"
PO_REQUIRED = "required"
PO_MUST_NOT_EXIST = "must_not_exist"


class OdooDecisionRuleDataError(ErpRepositoryError):
    pass


class OdooDecisionRuleMapper:
    """Map raw Odoo decision rule rows into canonical Hub rule contracts."""

    def __init__(
        self,
        *,
        mapping: OdooDecisionRuleFieldMapping,
        currency_repository: CurrencyReferenceRepository,
    ) -> None:
        self._mapping = mapping
        self._currency_repository = currency_repository

    def map_records(
        self,
        records: tuple[Mapping[str, Any], ...],
        *,
        company_id: int,
    ) -> tuple[InvoiceDecisionRule, ...]:
        applicable_records = tuple(
            record for record in records if self._is_applicable_active_record(record, company_id=company_id)
        )
        currency_codes = self._currency_codes(applicable_records)
        return tuple(
            self._rule_from_record(record, company_id=company_id, currency_codes=currency_codes)
            for record in applicable_records
        )

    def _currency_codes(self, records: tuple[Mapping[str, Any], ...]) -> dict[int, str]:
        currency_ids = tuple(
            sorted(
                {
                    currency_id
                    for record in records
                    if (currency_id := parse_odoo_many2one_id(record.get(self._mapping.currency))) is not None
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

    def _is_applicable_active_record(self, record: Mapping[str, Any], *, company_id: int | None) -> bool:
        active = _required_bool(record.get(self._mapping.active))
        if active is False:
            return False
        record_company_id = parse_odoo_many2one_id(record.get(self._mapping.company))
        if company_id is None:
            return True
        return record_company_id in {None, company_id}

    def _rule_from_record(
        self,
        record: Mapping[str, Any],
        *,
        company_id: int,
        currency_codes: dict[int, str],
    ) -> InvoiceDecisionRule:
        record_company_id = parse_odoo_many2one_id(record.get(self._mapping.company))
        if record_company_id not in {None, company_id}:
            raise OdooDecisionRuleDataError(SAFE_DECISION_RULE_DATA_ERROR)
        currency_id = parse_odoo_many2one_id(record.get(self._mapping.currency))
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
            vendor_partner_id=parse_odoo_many2one_id(record.get(self._mapping.vendor)),
            vendor_tax_id=_optional_text(record.get(self._mapping.vendor_tax_id)),
            currency_id=currency_id,
            currency_code=currency_codes.get(currency_id) if currency_id is not None else None,
            provider_document_type=_optional_text(record.get(self._mapping.provider_document_type)),
            purchase_order_present=parse_purchase_order_presence(record.get(self._mapping.purchase_order_present)),
            description_contains=parse_odoo_decision_rule_description_terms(
                record.get(self._mapping.description_contains)
            ),
            product_mapping_id=parse_odoo_many2one_id(record.get(self._mapping.product_mapping)),
            require_review=_required_bool(record.get(self._mapping.require_review)),
            require_business_context=_required_bool(record.get(self._mapping.require_business_context)),
            notes=_optional_text(record.get(self._mapping.notes)),
        ).to_invoice_decision_rule()


def parse_odoo_many2one_id(value: Any) -> int | None:
    if value in (None, False):
        return None
    reference_id = many2one_id(value)
    if reference_id is None or reference_id <= 0:
        raise OdooDecisionRuleDataError(SAFE_DECISION_RULE_DATA_ERROR)
    return reference_id


def parse_purchase_order_presence(value: Any) -> bool | None:
    if value in (None, False, PO_ANY):
        return None
    if value == PO_REQUIRED:
        return True
    if value == PO_MUST_NOT_EXIST:
        return False
    raise OdooDecisionRuleDataError(SAFE_DECISION_RULE_DATA_ERROR)


def parse_odoo_decision_rule_description_terms(value: Any) -> tuple[str, ...]:
    if value in (None, False):
        return ()
    if not isinstance(value, str):
        raise OdooDecisionRuleDataError(SAFE_DECISION_RULE_DATA_ERROR)
    return tuple(line.strip() for line in value.splitlines() if line.strip())


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


def _required_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OdooDecisionRuleDataError(SAFE_DECISION_RULE_DATA_ERROR)
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value in (None, False):
        return None
    return _required_text(value)
