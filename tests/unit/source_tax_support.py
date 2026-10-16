"""PR T W1 test support: source-tax ports for tests that are not about source tax.

``CLEAN_SOURCE_TAX_FACTS_READER`` answers like a verified, withholding-free, VAT-only
(0015) original document -- the shape of every one of today's production invoices --
so wiring it in leaves a test's behaviour exactly as before W1.
``ALLOW_SOURCE_TAX_SAFETY_CHECK`` is the real :class:`SourceTaxSafetyCheck` over it.
"""

from __future__ import annotations

from app.application.source_tax import SourceTaxAssessment
from app.application.source_tax.reader import SourceTaxSafetyCheck
from app.domain.invoice.tax_facts import SourceTaxFacts


def clean_source_tax_facts(invoice_uuid: str | None = None) -> SourceTaxFacts:
    return SourceTaxFacts(
        invoice_uuid=invoice_uuid,
        invoice_type_code="SATIS",
        tax_type_codes=("0015",),
        tax_subtotals_without_type_code=0,
        invoice_withholding_total_count=0,
        line_withholding_total_count=0,
    )


class CleanSourceTaxFactsReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str | None]] = []

    def assess(self, *, review_id: str, company_id: int, invoice_uuid: str | None) -> SourceTaxAssessment:
        self.calls.append((review_id, company_id, invoice_uuid))
        return SourceTaxAssessment.verified(clean_source_tax_facts(invoice_uuid))


class FixedSourceTaxFactsReader:
    """Answers every review with one fixed assessment (or raises ``error``)."""

    def __init__(self, assessment: SourceTaxAssessment | None = None, *, error: Exception | None = None) -> None:
        self.assessment = assessment
        self.error = error
        self.calls: list[tuple[str, int, str | None]] = []

    def assess(self, *, review_id: str, company_id: int, invoice_uuid: str | None) -> SourceTaxAssessment:
        self.calls.append((review_id, company_id, invoice_uuid))
        if self.error is not None:
            raise self.error
        assert self.assessment is not None
        return self.assessment


CLEAN_SOURCE_TAX_FACTS_READER = CleanSourceTaxFactsReader()
ALLOW_SOURCE_TAX_SAFETY_CHECK = SourceTaxSafetyCheck(facts_reader=CLEAN_SOURCE_TAX_FACTS_READER)
