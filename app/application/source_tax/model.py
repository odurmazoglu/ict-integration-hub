from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.domain.invoice.tax_facts import SourceTaxFacts


class SourceDocumentIssue(StrEnum):
    """Why a source document's tax facts cannot be trusted. Every value is deterministic:
    re-reading the same stored state gives the same answer (temporary failures raise
    :class:`~app.application.source_tax.exceptions.SourceTaxFactsUnavailableError` instead)."""

    #: A real Vendor Bill write was requested without any source-tax assessment.
    FACTS_NOT_SUPPLIED = "FACTS_NOT_SUPPLIED"
    DOCUMENT_MISSING = "DOCUMENT_MISSING"
    DOCUMENT_AMBIGUOUS = "DOCUMENT_AMBIGUOUS"
    HASH_MISMATCH = "HASH_MISMATCH"
    #: Malformed, unsafe (DTD / entity) or not a UBL ``Invoice``.
    DOCUMENT_INVALID = "DOCUMENT_INVALID"
    UUID_MISSING = "UUID_MISSING"
    UUID_MISMATCH = "UUID_MISMATCH"


@dataclass(frozen=True, slots=True)
class SourceTaxAssessment:
    """Verified tax facts of a review's original UBL document, or why there are none."""

    facts: SourceTaxFacts | None = None
    issue: SourceDocumentIssue | None = None

    def __post_init__(self) -> None:
        if (self.facts is None) == (self.issue is None):
            raise ValueError("A source tax assessment carries either verified facts or an issue.")

    @classmethod
    def verified(cls, facts: SourceTaxFacts) -> SourceTaxAssessment:
        return cls(facts=facts)

    @classmethod
    def unverified(cls, issue: SourceDocumentIssue) -> SourceTaxAssessment:
        return cls(issue=issue)

    @property
    def is_verified(self) -> bool:
        return self.facts is not None
