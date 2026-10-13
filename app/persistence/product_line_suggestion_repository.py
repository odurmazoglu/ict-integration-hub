"""PR F / F2: persistence of advisory product suggestion sets (ADR-0014).

Reads and writes only the two advisory tables; no other table is touched. Transaction
boundaries belong to the caller (one commit per line in the worker).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.advisory.product_suggestions import (
    ProductSuggestion,
    ProductSuggestionResult,
    SuggestionTier,
    combine_fingerprints,
)
from app.advisory.suggestion_sets import (
    DISPLAY_VALIDITY,
    AdvisoryLine,
    StoredSuggestionSet,
    SuggestionSetReason,
    SuggestionSetStatus,
)
from app.models.workbench_product_line_suggestion import (
    WorkbenchProductLineSuggestionCandidate,
    WorkbenchProductLineSuggestionSet,
)

_CURRENT = SuggestionSetStatus.CURRENT.value


class SqlAlchemyProductLineSuggestionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------ reads

    def list_current(self, *, company_id: int) -> tuple[StoredSuggestionSet, ...]:
        rows = self._session.scalars(
            select(WorkbenchProductLineSuggestionSet)
            .where(
                WorkbenchProductLineSuggestionSet.company_id == company_id,
                WorkbenchProductLineSuggestionSet.status == _CURRENT,
            )
            .order_by(WorkbenchProductLineSuggestionSet.id)
        ).all()
        return self._stored(rows)

    def displayable(
        self, *, company_id: int, review_id: str, review_version: int, now: datetime
    ) -> tuple[StoredSuggestionSet, ...]:
        """Current sets of one review version validated within ``DISPLAY_VALIDITY`` (F3 display)."""

        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware.")
        rows = self._session.scalars(
            select(WorkbenchProductLineSuggestionSet)
            .where(
                WorkbenchProductLineSuggestionSet.company_id == company_id,
                WorkbenchProductLineSuggestionSet.review_id == review_id,
                WorkbenchProductLineSuggestionSet.review_version == review_version,
                WorkbenchProductLineSuggestionSet.status == _CURRENT,
                WorkbenchProductLineSuggestionSet.validated_at >= now - DISPLAY_VALIDITY,
            )
            .order_by(WorkbenchProductLineSuggestionSet.line_number)
        ).all()
        return self._stored(rows)

    def count_history_older_than(self, *, company_id: int, cutoff: datetime) -> int:
        return int(
            self._session.scalar(
                select(func.count()).select_from(WorkbenchProductLineSuggestionSet).where(*_history(company_id, cutoff))
            )
            or 0
        )

    # ------------------------------------------------------------------ writes (caller commits)

    def insert_current(
        self,
        line: AdvisoryLine,
        result: ProductSuggestionResult,
        *,
        catalog_fingerprint: str,
        supplierinfo_fingerprint: str,
        engine_version: str,
        now: datetime,
    ) -> int:
        record = WorkbenchProductLineSuggestionSet(
            company_id=line.company_id,
            review_id=line.review_id,
            review_version=line.review_version,
            line_number=line.line_number,
            line_key=line.line_key,
            supplier_partner_id=line.supplier_partner_id,
            seller_item_code=line.seller_item_code,
            input_digest=line.input_digest,
            catalog_fingerprint=catalog_fingerprint,
            supplierinfo_fingerprint=supplierinfo_fingerprint,
            engine_version=engine_version,
            ambiguous=result.ambiguous,
            reclassification_recommended=result.reclassification_recommended,
            notes=list(result.notes),
            status=_CURRENT,
            status_reason=None,
            generated_at=now,
            validated_at=now,
            status_changed_at=None,
        )
        self._session.add(record)
        self._session.flush()
        for position, suggestion in enumerate(result.suggestions, start=1):
            self._session.add(
                WorkbenchProductLineSuggestionCandidate(
                    set_id=record.id,
                    position=position,
                    rank=suggestion.rank,
                    product_id=suggestion.product_id,
                    tier=suggestion.tier.value,
                    score=suggestion.score,
                    reasons=list(suggestion.reasons),
                    warnings=list(suggestion.warnings),
                )
            )
        self._session.flush()
        return record.id

    def retire(self, set_id: int, *, status: SuggestionSetStatus, reason: SuggestionSetReason, now: datetime) -> bool:
        """Compare-and-swap ``current`` -> ``status``; ``False`` when it was no longer current."""

        if status is SuggestionSetStatus.CURRENT:
            raise ValueError("retire() needs a non-current status.")
        updated = self._session.execute(
            update(WorkbenchProductLineSuggestionSet)
            .where(WorkbenchProductLineSuggestionSet.id == set_id, WorkbenchProductLineSuggestionSet.status == _CURRENT)
            .values(status=status.value, status_reason=reason.value, status_changed_at=now)
        )
        self._session.flush()
        return bool(updated.rowcount)

    def revalidate(self, set_ids: Iterable[int], *, now: datetime) -> int:
        ids = sorted(set(set_ids))
        if not ids:
            return 0
        updated = self._session.execute(
            update(WorkbenchProductLineSuggestionSet)
            .where(WorkbenchProductLineSuggestionSet.id.in_(ids), WorkbenchProductLineSuggestionSet.status == _CURRENT)
            .values(validated_at=now)
        )
        return int(updated.rowcount or 0)

    def delete_history_older_than(self, *, company_id: int, cutoff: datetime) -> int:
        ids = self._session.scalars(select(WorkbenchProductLineSuggestionSet.id).where(*_history(company_id, cutoff)))
        id_list = list(ids)
        if not id_list:
            return 0
        self._session.execute(
            delete(WorkbenchProductLineSuggestionCandidate).where(
                WorkbenchProductLineSuggestionCandidate.set_id.in_(id_list)
            )
        )
        self._session.execute(
            delete(WorkbenchProductLineSuggestionSet).where(WorkbenchProductLineSuggestionSet.id.in_(id_list))
        )
        return len(id_list)

    # ------------------------------------------------------------------ mapping

    def _stored(self, rows: Sequence[WorkbenchProductLineSuggestionSet]) -> tuple[StoredSuggestionSet, ...]:
        if not rows:
            return ()
        candidates: dict[int, list[WorkbenchProductLineSuggestionCandidate]] = {row.id: [] for row in rows}
        for candidate in self._session.scalars(
            select(WorkbenchProductLineSuggestionCandidate)
            .where(WorkbenchProductLineSuggestionCandidate.set_id.in_(list(candidates)))
            .order_by(WorkbenchProductLineSuggestionCandidate.set_id, WorkbenchProductLineSuggestionCandidate.position)
        ):
            candidates[candidate.set_id].append(candidate)
        return tuple(_stored(row, candidates[row.id]) for row in rows)


def _history(company_id: int, cutoff: datetime) -> tuple[object, ...]:
    return (
        WorkbenchProductLineSuggestionSet.company_id == company_id,
        WorkbenchProductLineSuggestionSet.status != _CURRENT,
        WorkbenchProductLineSuggestionSet.status_changed_at < cutoff,
    )


def _stored(
    row: WorkbenchProductLineSuggestionSet, candidates: Sequence[WorkbenchProductLineSuggestionCandidate]
) -> StoredSuggestionSet:
    fingerprint = combine_fingerprints(row.catalog_fingerprint, row.supplierinfo_fingerprint)
    result = ProductSuggestionResult(
        review_id=row.review_id,
        line_number=row.line_number,
        review_version=row.review_version,
        catalog_fingerprint=fingerprint,
        suggestions=tuple(
            ProductSuggestion(
                rank=candidate.rank,
                product_id=candidate.product_id,
                tier=SuggestionTier(candidate.tier),
                score=Decimal(candidate.score).quantize(Decimal("0.01")),
                reasons=tuple(candidate.reasons),
                warnings=tuple(candidate.warnings),
                review_version=row.review_version,
                catalog_fingerprint=fingerprint,
            )
            for candidate in candidates
        ),
        reclassification_recommended=row.reclassification_recommended,
        ambiguous=row.ambiguous,
        notes=tuple(row.notes or ()),
    )
    return StoredSuggestionSet(
        id=row.id,
        company_id=row.company_id,
        review_id=row.review_id,
        review_version=row.review_version,
        line_number=row.line_number,
        line_key=row.line_key,
        supplier_partner_id=row.supplier_partner_id,
        input_digest=row.input_digest,
        catalog_fingerprint=row.catalog_fingerprint,
        supplierinfo_fingerprint=row.supplierinfo_fingerprint,
        engine_version=row.engine_version,
        status=SuggestionSetStatus(row.status),
        status_reason=SuggestionSetReason(row.status_reason) if row.status_reason else None,
        generated_at=row.generated_at,
        validated_at=row.validated_at,
        result=result,
    )


def history_cutoff(now: datetime, retention: timedelta) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    return now - retention


__all__ = ["SqlAlchemyProductLineSuggestionRepository", "history_cutoff"]
