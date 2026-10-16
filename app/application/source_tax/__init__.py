"""PR T W1 withholding-tax / source-document safety guard.

Only the dependency-free modules are re-exported here (the rule engine imports them);
the storage-backed reader and the safety check live in
:mod:`app.application.source_tax.reader`.
"""

from app.application.source_tax.assessment import assess_source_document, source_tax_review_reasons
from app.application.source_tax.exceptions import (
    SourceDocumentUnreadableError,
    SourceTaxError,
    SourceTaxFactsUnavailableError,
    SourceTaxSafetyBlockedError,
)
from app.application.source_tax.model import SourceDocumentIssue, SourceTaxAssessment

__all__ = [
    "SourceDocumentIssue",
    "SourceDocumentUnreadableError",
    "SourceTaxAssessment",
    "SourceTaxError",
    "SourceTaxFactsUnavailableError",
    "SourceTaxSafetyBlockedError",
    "assess_source_document",
    "source_tax_review_reasons",
]
