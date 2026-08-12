from __future__ import annotations

from dataclasses import dataclass

from app.application.dto import ApplicationDTO
from app.application.rules import (
    InvoiceClassificationResult,
    InvoiceClassificationRuleEvidence,
    InvoiceClassificationStatus,
)
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workflow import WorkflowType
from app.billing.dto import CustomerInvoiceBillingInstruction
from app.domain.invoice import InternalInvoice
from app.matching import InvoiceProductMatchResult, PartnerMatchResult
from app.tax_mapping import InvoiceTaxMappingResult

REVIEW_CLASSIFICATION_EVIDENCE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ReviewExecutionEvidence(ApplicationDTO):
    """Immutable pre-decision evidence pinned to a Workbench review version."""

    review_id: str
    company_id: int
    review_version: int
    source_invoice_id: str
    invoice: InternalInvoice
    partner_match: PartnerMatchResult
    product_match: InvoiceProductMatchResult
    tax_match: InvoiceTaxMappingResult

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.review_version, "review_version must be positive.")
        _require_text(self.source_invoice_id, "source_invoice_id is required.")
        if not isinstance(self.invoice, InternalInvoice):
            raise WorkbenchContractError("InternalInvoice DTO is required.")
        if not isinstance(self.partner_match, PartnerMatchResult):
            raise WorkbenchContractError("PartnerMatchResult DTO is required.")
        if not isinstance(self.product_match, InvoiceProductMatchResult):
            raise WorkbenchContractError("InvoiceProductMatchResult DTO is required.")
        if not isinstance(self.tax_match, InvoiceTaxMappingResult):
            raise WorkbenchContractError("InvoiceTaxMappingResult DTO is required.")
        invoice_identity = self.invoice.header.ettn or self.invoice.header.invoice_uuid
        if self.source_invoice_id != invoice_identity:
            raise WorkbenchContractError("source_invoice_id must match InternalInvoice identity.")
        _validate_product_scope(self.invoice, self.product_match)
        _validate_tax_scope(self.invoice, self.tax_match)


@dataclass(frozen=True, slots=True)
class ReviewExecutionBillingEvidence(ApplicationDTO):
    """Immutable customer billing terms pinned to a Workbench review version."""

    review_id: str
    company_id: int
    review_version: int
    billing_instruction: CustomerInvoiceBillingInstruction

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.review_version, "review_version must be positive.")
        if not isinstance(self.billing_instruction, CustomerInvoiceBillingInstruction):
            raise WorkbenchContractError("CustomerInvoiceBillingInstruction DTO is required.")


@dataclass(frozen=True, slots=True)
class ReviewClassificationEvidence(ApplicationDTO):
    """Immutable deterministic classification evidence pinned to a review version."""

    review_id: str
    company_id: int
    review_version: int
    status: InvoiceClassificationStatus
    matched_rule_id: str | None = None
    matched_rule_code: str | None = None
    matched_rule_version: int | None = None
    matched_rule_name: str | None = None
    workflow: WorkflowType | None = None
    classification_code: str | None = None
    require_review: bool = False
    require_business_context: bool = False
    conflicting_rules: tuple[InvoiceClassificationRuleEvidence, ...] = ()
    schema_version: int = REVIEW_CLASSIFICATION_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.review_version, "review_version must be positive.")
        _require_positive_int(self.schema_version, "schema_version must be positive.")
        if self.schema_version != REVIEW_CLASSIFICATION_EVIDENCE_SCHEMA_VERSION:
            raise WorkbenchContractError("Classification evidence schema version is not supported.")
        if not isinstance(self.status, InvoiceClassificationStatus):
            raise WorkbenchContractError("Classification status is required.")
        if self.workflow is not None and not isinstance(self.workflow, WorkflowType):
            raise WorkbenchContractError("Classification workflow must be canonical.")
        if type(self.require_review) is not bool:
            raise WorkbenchContractError("require_review must be boolean.")
        if type(self.require_business_context) is not bool:
            raise WorkbenchContractError("require_business_context must be boolean.")
        conflicting_rules = tuple(self.conflicting_rules)
        for rule in conflicting_rules:
            if not isinstance(rule, InvoiceClassificationRuleEvidence):
                raise WorkbenchContractError("conflicting_rules must contain rule evidence.")
        object.__setattr__(
            self,
            "conflicting_rules",
            tuple(sorted(conflicting_rules, key=_classification_rule_sort_key)),
        )
        _validate_classification_status_shape(self)

    @classmethod
    def from_result(
        cls,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
        result: InvoiceClassificationResult,
    ) -> ReviewClassificationEvidence:
        if not isinstance(result, InvoiceClassificationResult):
            raise WorkbenchContractError("InvoiceClassificationResult DTO is required.")
        if result.status is InvoiceClassificationStatus.CONFLICT:
            return cls(
                review_id=review_id,
                company_id=company_id,
                review_version=review_version,
                status=result.status,
                conflicting_rules=result.conflict_rule_evidence,
            )
        if result.status is InvoiceClassificationStatus.NO_MATCH:
            return cls(
                review_id=review_id,
                company_id=company_id,
                review_version=review_version,
                status=result.status,
            )
        rule_evidence = _selected_rule_evidence(result)
        return cls(
            review_id=review_id,
            company_id=company_id,
            review_version=review_version,
            status=result.status,
            matched_rule_id=rule_evidence.rule_id,
            matched_rule_code=rule_evidence.rule_code,
            matched_rule_version=rule_evidence.rule_version,
            matched_rule_name=rule_evidence.rule_name,
            workflow=rule_evidence.workflow,
            classification_code=rule_evidence.classification_code,
            require_review=rule_evidence.require_review,
            require_business_context=rule_evidence.require_business_context,
        )


def _validate_product_scope(invoice: InternalInvoice, product_match: InvoiceProductMatchResult) -> None:
    invoice_line_numbers = tuple(line.line_number for line in invoice.lines)
    result_line_numbers = tuple(result.line_number for result in product_match.line_results)
    if result_line_numbers != invoice_line_numbers:
        raise WorkbenchContractError("Product match evidence must cover every invoice line in order.")


def _validate_tax_scope(invoice: InternalInvoice, tax_match: InvoiceTaxMappingResult) -> None:
    invoice_tax_keys = tuple(
        (line.line_number, tax_index) for line in invoice.lines for tax_index, _tax in enumerate(line.taxes)
    )
    result_tax_keys = tuple((result.line_number, result.tax_index) for result in tax_match.line_results)
    if result_tax_keys != invoice_tax_keys:
        raise WorkbenchContractError("Tax mapping evidence must cover every invoice tax in order.")


def _selected_rule_evidence(result: InvoiceClassificationResult) -> InvoiceClassificationRuleEvidence:
    if result.matched_rule_evidence:
        return result.matched_rule_evidence[0]
    if result.selected_rule is not None:
        return InvoiceClassificationRuleEvidence.from_rule(result.selected_rule)
    raise WorkbenchContractError("Matched classification evidence requires selected rule evidence.")


def _classification_rule_sort_key(rule: InvoiceClassificationRuleEvidence) -> tuple[str, int, str]:
    return rule.rule_code, rule.rule_version, rule.rule_id


def _validate_classification_status_shape(evidence: ReviewClassificationEvidence) -> None:
    has_matched_rule = any(
        value is not None
        for value in (
            evidence.matched_rule_id,
            evidence.matched_rule_code,
            evidence.matched_rule_version,
            evidence.matched_rule_name,
            evidence.workflow,
            evidence.classification_code,
        )
    )
    if evidence.status is InvoiceClassificationStatus.NO_MATCH:
        if (
            has_matched_rule
            or evidence.require_review
            or evidence.require_business_context
            or evidence.conflicting_rules
        ):
            raise WorkbenchContractError("NO_MATCH classification evidence must not include rule actions.")
        return
    if evidence.status in {InvoiceClassificationStatus.MATCHED, InvoiceClassificationStatus.REVIEW_REQUIRED}:
        _require_text(evidence.matched_rule_id, "matched_rule_id is required.")
        _require_text(evidence.matched_rule_code, "matched_rule_code is required.")
        _require_positive_int(evidence.matched_rule_version, "matched_rule_version must be positive.")
        _require_text(evidence.matched_rule_name, "matched_rule_name is required.")
        if evidence.conflicting_rules:
            raise WorkbenchContractError("Matched classification evidence must not include conflicts.")
        if evidence.status is InvoiceClassificationStatus.REVIEW_REQUIRED and not evidence.require_review:
            raise WorkbenchContractError("REVIEW_REQUIRED classification evidence must require review.")
        return
    if evidence.status is InvoiceClassificationStatus.CONFLICT:
        if has_matched_rule or evidence.require_review or evidence.require_business_context:
            raise WorkbenchContractError("CONFLICT classification evidence must not select a rule.")
        if len(evidence.conflicting_rules) < 2:
            raise WorkbenchContractError("CONFLICT classification evidence requires conflicting rules.")
        return
    raise WorkbenchContractError("Classification status is not supported.")


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(message)


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)
