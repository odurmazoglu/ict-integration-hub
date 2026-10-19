"""PR T T2-A: VAT exemption (code 335) mapping and its execution-time re-proof.

:class:`VatExemptionTaxPolicy` is used twice:

* classification (:class:`~app.application.rules.deterministic.DeterministicRuleEngine`):
  T1's fail-closed ``INVALID_INPUT`` results of an invoice are upgraded to one
  ``MATCHED`` / ``EXEMPTION`` result per line, only when the verified document proves the
  exemption and the configured Odoo tax passes validation;
* every Vendor Bill use (decision acceptance, preview, execution, retry and resume, through
  :class:`~app.application.source_tax.reader.SourceTaxSafetyCheck`): any line pinned to an
  exemption -- or to a configured exemption tax id -- is re-proven from the freshly verified
  document and a fresh read of the Odoo tax, never trusted from the pinned evidence.

Empty configuration (the default) never reads Odoo, leaves every T1 result untouched, and
blocks every exemption pin.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Protocol

from app.application.source_tax.exceptions import SourceTaxFactsUnavailableError, SourceTaxSafetyBlockedError
from app.application.source_tax.model import SourceTaxAssessment
from app.application.workflow import ManualReviewReasonCode
from app.domain.invoice import InternalInvoice
from app.domain.invoice.tax_facts import SourceTaxFacts
from app.erp.exceptions import ErpRepositoryError
from app.tax_mapping.exemption import (
    ZERO,
    assess_invoice_exemption,
    blocked_exemption_mapping,
    exemption_matched_by,
    exemption_tax_problems,
    is_exemption_pinned,
    upgrade_to_exemption,
    validate_vat_exemption_tax_config,
)
from app.tax_mapping.result import InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType

logger = logging.getLogger(__name__)

SAFE_EXEMPTION_BLOCKED_MESSAGE = "The invoice's VAT exemption mapping could not be re-proven safely."


class ExemptionTaxReader(Protocol):
    """Read-only ``account.tax`` lookup by exact id: the record, or ``None`` when absent.

    Raises ``ErpRepositoryError`` when Odoo cannot be read right now.
    """

    def read_exemption_tax(self, tax_id: int) -> Mapping[str, Any] | None:
        pass


class VatExemptionTaxPolicy:
    def __init__(
        self,
        *,
        tax_ids_by_code: Mapping[str, int] | None = None,
        tax_reader: ExemptionTaxReader | None = None,
    ) -> None:
        self._tax_ids_by_code = validate_vat_exemption_tax_config(dict(tax_ids_by_code or {}))
        if self._tax_ids_by_code and tax_reader is None:
            raise ValueError("A configured VAT exemption mapping requires an Odoo tax reader.")
        self._tax_reader = tax_reader

    @property
    def enabled(self) -> bool:
        return bool(self._tax_ids_by_code)

    @property
    def exemption_tax_ids(self) -> frozenset[int]:
        return frozenset(self._tax_ids_by_code.values())

    def map_invoice(
        self,
        invoice: InternalInvoice,
        tax_match: InvoiceTaxMappingResult,
        *,
        assessment: SourceTaxAssessment | None,
        company_id: int | None,
    ) -> InvoiceTaxMappingResult:
        """Classification step: ``tax_match`` itself unless the exemption is fully proven.

        An Odoo read failure propagates (``ErpRepositoryError``), exactly like a T1 tax
        lookup failure: nothing is classified, nothing is persisted.
        """

        if not self.enabled or assessment is None or assessment.facts is None:
            return tax_match
        eligibility = assess_invoice_exemption(invoice, assessment.facts)
        if not eligibility.eligible:
            return tax_match
        code = eligibility.code
        assert code is not None
        tax_id = self._tax_ids_by_code.get(code)
        if tax_id is None:
            return tax_match
        problems = exemption_tax_problems(
            self._read_tax(tax_id), expected_tax_id=tax_id, company_id=company_id, code=code
        )
        if problems:
            logger.warning(
                "vat_exemption_tax_invalid",
                extra={"company_id": company_id, "exemption_code": code, "tax_id": tax_id, "problems": problems},
            )
            return blocked_exemption_mapping(
                tax_match,
                reason=f"VAT exemption {code} is proven, but the configured Odoo tax failed validation.",
            )
        upgraded = upgrade_to_exemption(invoice, tax_match, code=code, tax_id=tax_id, company_id=company_id)
        if upgraded is None:
            return tax_match
        logger.info(
            "vat_exemption_tax_mapped",
            extra={"company_id": company_id, "exemption_code": code, "tax_id": tax_id},
        )
        return upgraded

    def ensure_pinned_exemptions_allowed(
        self,
        *,
        review_id: str,
        company_id: int,
        invoice: InternalInvoice,
        tax_match: InvoiceTaxMappingResult,
        facts: SourceTaxFacts,
    ) -> None:
        """Vendor Bill backstop. A no-op (and no Odoo read) when no line is exemption-pinned;
        otherwise raises :class:`SourceTaxSafetyBlockedError` unless the whole invoice is
        re-proven, or the retryable :class:`SourceTaxFactsUnavailableError`."""

        if not isinstance(tax_match, InvoiceTaxMappingResult):
            self._block(review_id, company_id, "Tax mapping evidence is missing.")
        results = tuple(line.result for line in tax_match.line_results)
        if not any(is_exemption_pinned(result, self.exemption_tax_ids) for result in results):
            return
        if not self.enabled:
            self._block(review_id, company_id, "VAT exemption mapping is not configured.")
        eligibility = assess_invoice_exemption(invoice, facts)
        if not eligibility.eligible:
            self._block(review_id, company_id, "Source document no longer proves the exemption.", eligibility.problems)
        code = eligibility.code
        assert code is not None
        tax_id = self._tax_ids_by_code.get(code)
        if tax_id is None:
            self._block(review_id, company_id, f"VAT exemption code {code} is not configured.")
        assert tax_id is not None
        keys = tuple((line.line_number, line.tax_index) for line in tax_match.line_results)
        expected_keys = tuple(
            (line.line_number, tax_index) for line in invoice.lines for tax_index, _tax in enumerate(line.taxes)
        )
        if tax_match.errors or keys != expected_keys:
            self._block(review_id, company_id, "Pinned tax mapping does not cover every invoice tax.")
        for result in results:
            if not _is_current_exemption_pin(result, code=code, tax_id=tax_id, company_id=company_id):
                self._block(review_id, company_id, "A pinned tax does not match the approved exemption mapping.")
        try:
            record = self._read_tax(tax_id)
        except ErpRepositoryError as exc:
            raise SourceTaxFactsUnavailableError("Configured VAT exemption tax could not be read right now.") from exc
        problems = exemption_tax_problems(record, expected_tax_id=tax_id, company_id=company_id, code=code)
        if problems:
            self._block(review_id, company_id, "Configured VAT exemption tax failed validation.", problems)

    def _read_tax(self, tax_id: int) -> Mapping[str, Any] | None:
        assert self._tax_reader is not None
        return self._tax_reader.read_exemption_tax(tax_id)

    @staticmethod
    def _block(review_id: str, company_id: int, message: str, problems: tuple[str, ...] = ()) -> None:
        logger.warning(
            "vat_exemption_vendor_bill_blocked",
            extra={"review_id": review_id, "company_id": company_id, "reason": message, "problems": problems},
        )
        raise SourceTaxSafetyBlockedError(
            f"{SAFE_EXEMPTION_BLOCKED_MESSAGE} {message}",
            reason_codes=(ManualReviewReasonCode.UNSUPPORTED_INVOICE_CONTENT,),
        )


def _is_current_exemption_pin(result: TaxMatchResult, *, code: str, tax_id: int, company_id: int) -> bool:
    return (
        result.status is TaxMatchStatus.MATCHED
        and result.tax_type is TaxType.EXEMPTION
        and result.tax_id == tax_id
        and result.company_id == company_id
        and result.tax_rate is not None
        and result.tax_rate == ZERO
        and result.matched_by == exemption_matched_by(code)
    )


#: Empty configuration: never reads Odoo, maps nothing, blocks every exemption pin.
DISABLED_VAT_EXEMPTION_POLICY = VatExemptionTaxPolicy()
