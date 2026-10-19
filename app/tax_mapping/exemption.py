"""PR T T2-A: deterministic UBL-TR VAT exemption mapping (code 335 only).

Pure policy, no I/O. A Vendor Bill line may carry an exemption tax only when the
*verified original document* (hash + UUID, :mod:`app.application.source_tax`) proves the
exemption -- never from the canonical evidence's free-text exemption reason, which the
parser prefers over the code -- and only to the one ERP purchase tax id an accountant
approved for that code (``VAT_EXEMPTION_PURCHASE_TAX_IDS``). The tax is never discovered
by name or rate: the configured id is the identity, the ``335`` name token is only an
extra guard against configuring the wrong (e.g. generic 0%) tax.

v1 is all-lines: every invoice line must be a single, aligned ``0015`` / ``335`` /
zero-amount subtotal, or no line is mapped and T1's fail-closed result stands.

The document must also be structurally complete (M1): no tax subtotal or total outside the
canonical paths, every ``TaxTotal/TaxAmount`` exactly 0, an invoice-level ``TaxTotal``, one
``LegalMonetaryTotal`` whose tax-exclusive and tax-inclusive amounts (when present) are equal
and match canonical evidence.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.domain.invoice import InternalInvoice, Tax
from app.domain.invoice.tax_facts import (
    SUPPORTED_TAX_TYPE_CODES,
    SourceTaxFacts,
    SourceTaxSubtotalFact,
    SourceTaxTotalFact,
)
from app.tax_mapping.engine import EXACT_MATCH_CONFIDENCE, normalize_tax_type
from app.tax_mapping.result import (
    InvoiceTaxLineResult,
    InvoiceTaxMappingResult,
    TaxMatchResult,
    TaxMatchStatus,
    TaxType,
)

#: Exemption codes accounting approved for automatic mapping. 209 and 351 are NOT approved.
APPROVED_VAT_EXEMPTION_CODES = frozenset({"335"})
#: The only GİB ``InvoiceTypeCode`` an exemption mapping applies to.
EXEMPTION_INVOICE_TYPE_CODE = "ISTISNA"
#: Generic 0% purchase tax of the Turkish chart (``l10n_tr`` ``tr_p_0``) on the production
#: company. Never an exemption tax: its reporting is not the approved treatment of any code.
FORBIDDEN_EXEMPTION_TAX_IDS = frozenset({6})
EXEMPTION_MATCHED_BY_PREFIX = "source_exemption_code:"
ZERO = Decimal("0")

#: Fields of ``account.tax`` the validation needs; every one must be present in the read.
EXEMPTION_TAX_FIELDS = (
    "id",
    "name",
    "active",
    "company_id",
    "country_code",
    "type_tax_use",
    "amount_type",
    "amount",
    "children_tax_ids",
    "price_include",
    "price_include_override",
    "include_base_amount",
    "tax_exigibility",
    "has_negative_factor",
    "fiscal_position_ids",
    "original_tax_ids",
    "replacing_tax_ids",
)
_EMPTY_RELATION_FIELDS = ("children_tax_ids", "fiscal_position_ids", "original_tax_ids", "replacing_tax_ids")
#: ``price_include_override`` values compatible with a tax-excluded price (unset = company default).
_TAX_EXCLUDED_OVERRIDES = (False, None, "tax_excluded")
_EXPECTED_COUNTRY_CODE = "TR"


class VatExemptionConfigError(ValueError):
    """``VAT_EXEMPTION_PURCHASE_TAX_IDS`` is not a valid, approved configuration."""


def validate_vat_exemption_tax_config(value: object) -> dict[str, int]:
    """The validated ``{"<code>": <tax id>}`` mapping; raises :class:`VatExemptionConfigError`."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise VatExemptionConfigError("VAT exemption tax configuration must be a JSON object.")
    validated: dict[str, int] = {}
    for raw_code, raw_tax_id in value.items():
        if not isinstance(raw_code, str) or raw_code != raw_code.strip() or not raw_code:
            raise VatExemptionConfigError("VAT exemption codes must be exact, non-empty strings.")
        if raw_code not in APPROVED_VAT_EXEMPTION_CODES:
            raise VatExemptionConfigError(f"VAT exemption code {raw_code} is not approved for automatic mapping.")
        if type(raw_tax_id) is not int or raw_tax_id <= 0:
            raise VatExemptionConfigError(f"VAT exemption code {raw_code} needs a positive integer ERP tax id.")
        if raw_tax_id in FORBIDDEN_EXEMPTION_TAX_IDS:
            raise VatExemptionConfigError("The generic 0% purchase tax is never a VAT exemption tax.")
        if raw_tax_id in validated.values():
            raise VatExemptionConfigError("Each VAT exemption code needs its own dedicated ERP tax id.")
        validated[raw_code] = raw_tax_id
    return validated


@dataclass(frozen=True, slots=True)
class ExemptionEligibility:
    """Whether every line of an invoice is provably exempt under one approved code."""

    code: str | None
    problems: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return self.code is not None and not self.problems


def assess_invoice_exemption(invoice: InternalInvoice, facts: SourceTaxFacts | None) -> ExemptionEligibility:
    """The all-lines v1 policy: ``ISTISNA`` + one aligned ``0015`` / approved-code /
    zero-amount subtotal per line, consistent invoice-level subtotals, no withholding
    and no non-VAT tax content."""

    problems = _document_problems(invoice, facts)
    if problems:
        return ExemptionEligibility(code=None, problems=problems)
    assert facts is not None
    problems = _completeness_problems(invoice, facts) or _alignment_problems(invoice, facts)
    if problems:
        return ExemptionEligibility(code=None, problems=problems)
    codes = {fact.exemption_reason_code for fact in facts.line_tax_subtotals}
    problems = tuple(problem for fact in facts.line_tax_subtotals for problem in _exempt_fact_problems(fact))
    if not problems and len(codes) != 1:
        problems = ("Every line must carry the same exemption code.",)
    code = next(iter(codes)) if len(codes) == 1 else None
    if not problems and code is not None:
        problems = _invoice_level_problems(facts, code=code)
    return ExemptionEligibility(code=code if not problems else None, problems=problems)


def _document_problems(invoice: InternalInvoice, facts: SourceTaxFacts | None) -> tuple[str, ...]:
    if not isinstance(invoice, InternalInvoice):
        return ("Canonical invoice is required.",)
    if facts is None:
        return ("Verified source tax facts are required.",)
    problems: list[str] = []
    if facts.has_withholding:
        problems.append("Withholding tax content is never mapped as an exemption.")
    if facts.has_unsupported_tax_content:
        problems.append("Non-VAT tax content is never mapped as an exemption.")
    if facts.invoice_type_code != EXEMPTION_INVOICE_TYPE_CODE:
        problems.append("InvoiceTypeCode must be ISTISNA.")
    canonical_type = (invoice.header.invoice_type or "").strip().upper()
    if canonical_type != EXEMPTION_INVOICE_TYPE_CODE:
        problems.append("Canonical invoice type must be ISTISNA.")
    if facts.line_ids is None:
        problems.append("Source line structure was not extracted.")
    return tuple(problems)


def _completeness_problems(invoice: InternalInvoice, facts: SourceTaxFacts) -> tuple[str, ...]:
    """Prove nothing tax-relevant exists outside what the exact traversal (and so the
    canonical evidence) sees, and that the document's own totals carry no tax."""

    if facts.unrecognized_tax_subtotal_count is None or facts.unrecognized_tax_total_count is None:
        return ("Source tax structure completeness was not extracted.",)
    problems: list[str] = []
    if facts.unrecognized_tax_subtotal_count:
        problems.append("Source document has TaxSubtotal elements outside the canonical tax paths.")
    if facts.unrecognized_tax_total_count:
        problems.append("Source document has TaxTotal elements outside the canonical tax paths.")
    if not facts.invoice_tax_totals:
        problems.append("Source document has no invoice-level TaxTotal.")
    problems.extend(
        problem for total in facts.line_tax_totals + facts.invoice_tax_totals for problem in _tax_total_problems(total)
    )
    problems.extend(_monetary_total_problems(invoice, facts))
    return tuple(problems)


def _tax_total_problems(total: SourceTaxTotalFact) -> tuple[str, ...]:
    where = f"line {total.line_id}" if total.line_id is not None else "invoice level"
    if total.malformed:
        return (f"TaxTotal of {where} has a malformed TaxAmount.",)
    if not total.amount_present or total.amount is None:
        return (f"TaxTotal of {where} has no TaxAmount.",)
    if total.amount != ZERO:
        return (f"TaxTotal of {where} must have TaxAmount 0.",)
    if total.subtotal_count == 0:
        return (f"TaxTotal of {where} has no TaxSubtotal.",)
    return ()


def _monetary_total_problems(invoice: InternalInvoice, facts: SourceTaxFacts) -> tuple[str, ...]:
    if facts.monetary_total_count != 1:
        return ("Source document must have exactly one LegalMonetaryTotal.",)
    if facts.monetary_totals_malformed:
        return ("Source document has a malformed monetary total.",)
    exclusive, inclusive = facts.tax_exclusive_amount, facts.tax_inclusive_amount
    if exclusive is not None and inclusive is not None and exclusive != inclusive:
        return ("TaxInclusiveAmount must equal TaxExclusiveAmount for a VAT-exempt invoice.",)
    totals = invoice.totals
    if not _same_decimal(totals.tax_exclusive_amount, exclusive) or not _same_decimal(
        totals.tax_inclusive_amount, inclusive
    ):
        return ("Source monetary totals do not match canonical evidence.",)
    return ()


def _alignment_problems(invoice: InternalInvoice, facts: SourceTaxFacts) -> tuple[str, ...]:
    canonical_ids = tuple(line.line_number for line in invoice.lines)
    if not canonical_ids:
        return ("Invoice has no lines.",)
    if facts.line_ids != canonical_ids:
        return ("Source invoice lines do not align with canonical evidence.",)
    if any(line_id is None or not line_id.strip() for line_id in canonical_ids):
        return ("Every invoice line needs an identifier.",)
    if len(set(canonical_ids)) != len(canonical_ids):
        return ("Invoice line identifiers must be unique.",)
    canonical_keys = tuple(
        (line.line_number, tax_index) for line in invoice.lines for tax_index, _tax in enumerate(line.taxes)
    )
    fact_keys = tuple((fact.line_id, fact.tax_index) for fact in facts.line_tax_subtotals)
    if fact_keys != canonical_keys:
        return ("Source tax subtotals do not align with canonical evidence.",)
    if any(len(line.taxes) != 1 for line in invoice.lines):
        return ("Every line must carry exactly one tax subtotal.",)
    canonical_taxes = tuple(line.taxes[0] for line in invoice.lines)
    for tax, fact in zip(canonical_taxes, facts.line_tax_subtotals, strict=True):
        if not _canonical_tax_matches(tax, fact):
            return (f"Source tax of line {fact.line_id} does not match canonical evidence.",)
    return ()


def _canonical_tax_matches(tax: Tax, fact: SourceTaxSubtotalFact) -> bool:
    return (
        normalize_tax_type(tax.tax_type) is TaxType.VAT
        and _same_decimal(tax.rate, fact.percent)
        and _same_decimal(tax.tax_amount, fact.tax_amount)
        and _same_decimal(tax.base_amount, fact.taxable_amount)
        # The parser keeps the reason text and falls back to the code (``_first_text``).
        and tax.exemption_reason == (fact.exemption_reason or fact.exemption_reason_code)
    )


def _same_decimal(left: Decimal | None, right: Decimal | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return left == right


def _exempt_fact_problems(fact: SourceTaxSubtotalFact) -> tuple[str, ...]:
    where = f"line {fact.line_id}" if fact.line_id is not None else "invoice level"
    if not fact.has_subtotal:
        return (f"Tax of {where} has no TaxSubtotal.",)
    if fact.malformed:
        return (f"Tax of {where} has a malformed amount.",)
    problems: list[str] = []
    if fact.tax_type_code not in SUPPORTED_TAX_TYPE_CODES:
        problems.append(f"Tax of {where} is not TaxTypeCode 0015.")
    if fact.exemption_reason_code is None:
        problems.append(f"Tax of {where} has no TaxExemptionReasonCode.")
    elif fact.exemption_reason_code not in APPROVED_VAT_EXEMPTION_CODES:
        problems.append(f"Exemption code {fact.exemption_reason_code} of {where} is not approved.")
    if fact.tax_amount is None or fact.tax_amount != ZERO:
        problems.append(f"Tax of {where} must have TaxAmount 0.")
    if fact.percent_present and (fact.percent is None or fact.percent != ZERO):
        problems.append(f"Tax of {where} must have no Percent or Percent 0.")
    if fact.taxable_amount is None or fact.taxable_amount < ZERO:
        problems.append(f"Tax of {where} needs a non-negative TaxableAmount.")
    return tuple(problems)


def _invoice_level_problems(facts: SourceTaxFacts, *, code: str) -> tuple[str, ...]:
    subtotals = facts.invoice_tax_subtotals
    if not subtotals:
        return ("Source document has no invoice-level TaxSubtotal.",)
    problems = [problem for fact in subtotals for problem in _exempt_fact_problems(fact)]
    if problems:
        return tuple(problems)
    if any(fact.exemption_reason_code != code for fact in subtotals):
        return ("Invoice-level exemption code differs from the line exemption code.",)
    invoice_base = sum((fact.taxable_amount or ZERO for fact in subtotals), ZERO)
    line_base = sum((fact.taxable_amount or ZERO for fact in facts.line_tax_subtotals), ZERO)
    if invoice_base != line_base:
        return ("Invoice-level TaxableAmount does not equal the sum of the line TaxableAmounts.",)
    return ()


def exemption_tax_problems(
    record: Mapping[str, Any] | None,
    *,
    expected_tax_id: int,
    company_id: int | None,
    code: str,
) -> tuple[str, ...]:
    """Why the configured ERP tax may not carry exemption ``code`` (empty: it may).

    Only ``account.tax``'s own fields are observable through the Hub's allowlisted ERP client;
    repartition lines, tax report tags, tax accounts and the tax group's accounts are NOT,
    so they are never claimed as validated here (accounting confirms them in the ERP).
    """

    if expected_tax_id in FORBIDDEN_EXEMPTION_TAX_IDS:
        return ("The generic 0% purchase tax is never a VAT exemption tax.",)
    if record is None:
        return ("Configured exemption tax was not found in the ERP.",)
    missing = tuple(field for field in EXEMPTION_TAX_FIELDS if field not in record)
    if missing:
        return ("Configured exemption tax is missing required fields: " + ", ".join(missing) + ".",)
    problems: list[str] = []
    if type(record["id"]) is not int or record["id"] != expected_tax_id:
        problems.append("Configured exemption tax id does not match the ERP record.")
    if record["active"] is not True:
        problems.append("Configured exemption tax is not active.")
    if company_id is None or _many2one_id(record["company_id"]) != company_id:
        problems.append("Configured exemption tax belongs to another company.")
    if record["country_code"] != _EXPECTED_COUNTRY_CODE:
        problems.append("Configured exemption tax is not a Türkiye tax.")
    if record["type_tax_use"] != "purchase":
        problems.append("Configured exemption tax is not a purchase tax.")
    if record["amount_type"] != "percent":
        problems.append("Configured exemption tax is not a percentage tax.")
    if not _is_zero_amount(record["amount"]):
        problems.append("Configured exemption tax amount is not 0.")
    for field in _EMPTY_RELATION_FIELDS:
        if record[field] != []:
            problems.append(f"Configured exemption tax must have no {field}.")
    if record["price_include"] is not False:
        problems.append("Configured exemption tax must not be included in price.")
    if record["price_include_override"] not in _TAX_EXCLUDED_OVERRIDES:
        problems.append("Configured exemption tax price inclusion must be tax excluded.")
    if record["include_base_amount"] is not False:
        problems.append("Configured exemption tax must not affect the base of subsequent taxes.")
    if record["tax_exigibility"] != "on_invoice":
        problems.append("Configured exemption tax must be due on invoice.")
    if record["has_negative_factor"] is not False:
        problems.append("Configured exemption tax must have no negative repartition factor.")
    if not _has_code_token(record["name"], code):
        problems.append(f"Configured exemption tax name must carry the {code} token.")
    return tuple(problems)


def _many2one_id(value: object) -> int | None:
    if isinstance(value, list | tuple) and value and type(value[0]) is int:
        return value[0]
    if type(value) is int:
        return value
    return None


def _is_zero_amount(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return Decimal(str(value)) == ZERO


def _has_code_token(name: object, code: str) -> bool:
    if not isinstance(name, str):
        return False
    return re.search(rf"(?<![0-9A-Za-z]){re.escape(code)}(?![0-9A-Za-z])", name) is not None


def exemption_matched_by(code: str) -> str:
    return f"{EXEMPTION_MATCHED_BY_PREFIX}{code}"


def exemption_match_result(*, code: str, tax_id: int, company_id: int | None) -> TaxMatchResult:
    return TaxMatchResult(
        status=TaxMatchStatus.MATCHED,
        tax_id=tax_id,
        company_id=company_id,
        tax_type=TaxType.EXEMPTION,
        tax_rate=ZERO,
        matched_by=exemption_matched_by(code),
        confidence=EXACT_MATCH_CONFIDENCE,
        reason=f"Verified UBL-TR exemption code {code} mapped to the approved ERP purchase tax.",
        candidate_count=1,
    )


def upgrade_to_exemption(
    invoice: InternalInvoice,
    tax_match: InvoiceTaxMappingResult,
    *,
    code: str,
    tax_id: int,
    company_id: int | None,
) -> InvoiceTaxMappingResult | None:
    """Every line's T1 ``INVALID_INPUT`` result replaced by the exemption match, or ``None``
    when ``tax_match`` is not exactly the all-blocked shape the policy expects."""

    expected_keys = tuple(
        (line.line_number, tax_index) for line in invoice.lines for tax_index, _tax in enumerate(line.taxes)
    )
    keys = tuple((line.line_number, line.tax_index) for line in tax_match.line_results)
    if tax_match.errors or keys != expected_keys:
        return None
    if any(line.result.status is not TaxMatchStatus.INVALID_INPUT for line in tax_match.line_results):
        return None
    result = exemption_match_result(code=code, tax_id=tax_id, company_id=company_id)
    return InvoiceTaxMappingResult(
        line_results=tuple(
            InvoiceTaxLineResult(line_number=line.line_number, tax_index=line.tax_index, result=result)
            for line in tax_match.line_results
        ),
        warnings=tax_match.warnings,
        errors=(),
    )


def blocked_exemption_mapping(tax_match: InvoiceTaxMappingResult, *, reason: str) -> InvoiceTaxMappingResult:
    """``tax_match`` with every ``INVALID_INPUT`` reason replaced by ``reason`` (status kept)."""

    return InvoiceTaxMappingResult(
        line_results=tuple(
            InvoiceTaxLineResult(
                line_number=line.line_number,
                tax_index=line.tax_index,
                result=_with_reason(line.result, reason)
                if line.result.status is TaxMatchStatus.INVALID_INPUT
                else line.result,
            )
            for line in tax_match.line_results
        ),
        warnings=tax_match.warnings,
        errors=tax_match.errors,
    )


def _with_reason(result: TaxMatchResult, reason: str) -> TaxMatchResult:
    return TaxMatchResult(
        status=result.status,
        tax_id=result.tax_id,
        company_id=result.company_id,
        tax_type=result.tax_type,
        tax_rate=result.tax_rate,
        matched_by=result.matched_by,
        confidence=result.confidence,
        reason=reason,
        candidate_count=result.candidate_count,
    )


def is_exemption_pinned(result: TaxMatchResult, exemption_tax_ids: frozenset[int]) -> bool:
    """A result that claims an exemption, or points at any configured exemption tax."""

    return (
        result.tax_type is TaxType.EXEMPTION
        or (result.matched_by or "").startswith(EXEMPTION_MATCHED_BY_PREFIX)
        or (result.tax_id is not None and result.tax_id in exemption_tax_ids)
    )
