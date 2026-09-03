from __future__ import annotations

from typing import Protocol

from app.application.quotation.contracts import QuotationScenarioSnapshot
from app.application.quotation.exceptions import QuotationEvidenceError

QUOTATION_SCENARIO_EVIDENCE_SCHEMA_VERSION = 1


class QuotationScenarioEvidenceRepository(Protocol):
    """Application-facing port for immutable Hub-owned quotation scenario evidence.

    Semantic identity is ``(company_id, review_id, decision_id, decision_version,
    scenario_id)``. First persistence inserts immutable evidence. An exact replay
    of an equivalent snapshot is idempotent and returns the stored evidence. A
    replay carrying the same semantic identity but a different commercial snapshot
    fails closed. Stored evidence is never updated in place.
    """

    def persist(self, snapshot: QuotationScenarioSnapshot) -> QuotationScenarioSnapshot:
        pass

    def get(
        self,
        *,
        company_id: int,
        review_id: str,
        decision_id: str,
        decision_version: int,
        scenario_id: str,
    ) -> QuotationScenarioSnapshot:
        pass


class PersistQuotationScenarioEvidenceUseCase:
    """Persist a captured quotation scenario snapshot as immutable Hub evidence.

    The snapshot is already the canonical, ERP-independent capture produced by
    :class:`app.application.quotation.capture.CaptureQuotationScenarioUseCase`.
    This use case performs no Odoo authoring re-read and no ``sale.order`` write;
    it only writes durable evidence that future quotation execution and retries
    consume instead of mutable Odoo Proposal Scenario records.
    """

    def __init__(self, *, repository: QuotationScenarioEvidenceRepository) -> None:
        self._repository = repository

    def execute(self, snapshot: QuotationScenarioSnapshot) -> QuotationScenarioSnapshot:
        if not isinstance(snapshot, QuotationScenarioSnapshot):
            raise QuotationEvidenceError("canonical QuotationScenarioSnapshot is required.")
        return self._repository.persist(snapshot)
