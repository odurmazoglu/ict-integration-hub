"""PR F / F2 + F3.1: persistence of advisory product suggestion sets (ADR-0014).

Reads and writes only the two advisory tables; no other table is touched. Transaction
boundaries belong to the caller (one commit per line in the worker).

F3.1 refresh marker (``display_synced_at``): a set whose display state changes -- created,
retired while it may still be shown, or revalidated after it was cleared -- gets ``NULL``,
which makes the database-only display sweep refresh its review once. An already-cleared
set keeps its marker, so it is never refreshed again. Nothing here ever moves
``validated_at`` except :meth:`revalidate` after a successful catalog read.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.advisory.product_suggestions import (
    ProductSuggestion,
    ProductSuggestionResult,
    SuggestionTier,
    combine_fingerprints,
)
from app.advisory.suggestion_sets import (
    DISPLAY_VALIDITY,
    PROJECTION_CUTOFF,
    AdvisoryLine,
    StoredSuggestionSet,
    SuggestionSetReason,
    SuggestionSetStatus,
    display_cleared,
    needs_display_refresh,
)
from app.models.workbench_product_line_suggestion import (
    WorkbenchProductLineSuggestionCandidate,
    WorkbenchProductLineSuggestionSet,
)

_CURRENT = SuggestionSetStatus.CURRENT.value
#: Column width of ``product_name``; a longer Odoo display name is truncated, never rejected.
PRODUCT_NAME_MAX_LENGTH = 512


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

    def list_current_for_review(self, *, company_id: int, review_id: str) -> tuple[StoredSuggestionSet, ...]:
        """Every current set of one review, any version (F3.1: the display gate decides)."""

        rows = self._session.scalars(
            select(WorkbenchProductLineSuggestionSet)
            .where(
                WorkbenchProductLineSuggestionSet.company_id == company_id,
                WorkbenchProductLineSuggestionSet.review_id == review_id,
                WorkbenchProductLineSuggestionSet.status == _CURRENT,
            )
            .order_by(WorkbenchProductLineSuggestionSet.id)
        ).all()
        return self._stored(rows)

    def display_refresh_due(self, *, company_id: int, now: datetime) -> tuple[StoredSuggestionSet, ...]:
        """F3.1: sets whose Odoo display may differ from what the Hub gate allows (database only)."""

        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware.")
        rows = self._session.scalars(
            select(WorkbenchProductLineSuggestionSet)
            .where(
                WorkbenchProductLineSuggestionSet.company_id == company_id,
                or_(
                    WorkbenchProductLineSuggestionSet.display_synced_at.is_(None),
                    (WorkbenchProductLineSuggestionSet.status == _CURRENT)
                    & (WorkbenchProductLineSuggestionSet.validated_at < now - PROJECTION_CUTOFF),
                ),
            )
            .order_by(WorkbenchProductLineSuggestionSet.id)
        ).all()
        return tuple(stored for stored in self._stored(rows) if needs_display_refresh(stored, now=now))

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
        product_names: Mapping[int, str] | None = None,
    ) -> int:
        names = product_names or {}
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
            display_synced_at=None,
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
                    product_name=_name(names.get(suggestion.product_id)),
                )
            )
        self._session.flush()
        return record.id

    def retire(self, set_id: int, *, status: SuggestionSetStatus, reason: SuggestionSetReason, now: datetime) -> bool:
        """Compare-and-swap ``current`` -> ``status``; ``False`` when it was no longer current."""

        if status is SuggestionSetStatus.CURRENT:
            raise ValueError("retire() needs a non-current status.")
        values: dict[str, object] = {"status": status.value, "status_reason": reason.value, "status_changed_at": now}
        marker = self._session.execute(
            select(
                WorkbenchProductLineSuggestionSet.validated_at, WorkbenchProductLineSuggestionSet.display_synced_at
            ).where(WorkbenchProductLineSuggestionSet.id == set_id)
        ).first()
        if marker is not None and not display_cleared(marker.validated_at, marker.display_synced_at):
            # It may still be shown in Odoo: the display sweep must clear it once.
            values["display_synced_at"] = None
        updated = self._session.execute(
            update(WorkbenchProductLineSuggestionSet)
            .where(WorkbenchProductLineSuggestionSet.id == set_id, WorkbenchProductLineSuggestionSet.status == _CURRENT)
            .values(**values)
        )
        self._session.flush()
        return bool(updated.rowcount)

    def revalidate(self, set_ids: Iterable[int], *, now: datetime) -> int:
        ids = sorted(set(set_ids))
        if not ids:
            return 0
        markers = self._session.execute(
            select(
                WorkbenchProductLineSuggestionSet.id,
                WorkbenchProductLineSuggestionSet.validated_at,
                WorkbenchProductLineSuggestionSet.display_synced_at,
            ).where(WorkbenchProductLineSuggestionSet.id.in_(ids))
        ).all()
        # A set the sweep already cleared becomes showable again: refresh its review once.
        cleared = sorted(row.id for row in markers if display_cleared(row.validated_at, row.display_synced_at))
        updated = 0
        for group, values in (
            (sorted(set(ids) - set(cleared)), {"validated_at": now}),
            (cleared, {"validated_at": now, "display_synced_at": None}),
        ):
            if not group:
                continue
            result = self._session.execute(
                update(WorkbenchProductLineSuggestionSet)
                .where(
                    WorkbenchProductLineSuggestionSet.id.in_(group),
                    WorkbenchProductLineSuggestionSet.status == _CURRENT,
                )
                .values(**values)
            )
            updated += int(result.rowcount or 0)
        return updated

    def mark_display_synced(self, sets: Iterable[StoredSuggestionSet], *, synced_at: datetime) -> int:
        """F3.1: record a successful refresh that started at ``synced_at`` (compare-and-set).

        Only a set whose status and ``validated_at`` are still the ones the sweep read is
        marked, so a concurrent retirement or revalidation is never hidden. ``validated_at``
        itself is never touched.
        """

        if synced_at.tzinfo is None:
            raise ValueError("synced_at must be timezone-aware.")
        marked = 0
        for stored in sets:
            result = self._session.execute(
                update(WorkbenchProductLineSuggestionSet)
                .where(
                    WorkbenchProductLineSuggestionSet.id == stored.id,
                    WorkbenchProductLineSuggestionSet.status == stored.status.value,
                    WorkbenchProductLineSuggestionSet.validated_at == stored.validated_at,
                )
                .values(display_synced_at=synced_at)
            )
            marked += int(result.rowcount or 0)
        return marked

    def reset_display_markers(self, *, company_id: int) -> int:
        """F3.6b: forget the recorded Odoo refresh of every *current* set of ``company_id``.

        Run by ``clear-display`` only after Odoo was verified empty: every current set becomes
        due again, so re-enabling display republishes it once. Compare-and-set on
        ``status='current'`` and a non-null marker; superseded/invalidated history and
        ``validated_at`` are never touched.
        """

        result = self._session.execute(
            update(WorkbenchProductLineSuggestionSet)
            .where(
                WorkbenchProductLineSuggestionSet.company_id == company_id,
                WorkbenchProductLineSuggestionSet.status == _CURRENT,
                WorkbenchProductLineSuggestionSet.display_synced_at.is_not(None),
            )
            .values(display_synced_at=None)
        )
        self._session.flush()
        return int(result.rowcount or 0)

    def count_display_marked_current(self, *, company_id: int) -> int:
        """F3.6b: current sets carrying a display marker (only a display-enabled refresh sets one)."""

        return int(
            self._session.scalar(
                select(func.count())
                .select_from(WorkbenchProductLineSuggestionSet)
                .where(
                    WorkbenchProductLineSuggestionSet.company_id == company_id,
                    WorkbenchProductLineSuggestionSet.status == _CURRENT,
                    WorkbenchProductLineSuggestionSet.display_synced_at.is_not(None),
                )
            )
            or 0
        )

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
        seller_item_code=row.seller_item_code,
        display_synced_at=row.display_synced_at,
        product_names=tuple(
            (candidate.product_id, candidate.product_name) for candidate in candidates if candidate.product_name
        ),
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


def _name(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:PRODUCT_NAME_MAX_LENGTH]


def history_cutoff(now: datetime, retention: timedelta) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    return now - retention


__all__ = ["SqlAlchemyProductLineSuggestionRepository", "history_cutoff"]
