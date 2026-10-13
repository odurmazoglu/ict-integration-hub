"""PR F / F3.1: display adapter for persisted advisory product suggestions (ADR-0014).

Two read-mostly pieces, both behind ``ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTIONS_DISPLAY_ENABLED``:

* :class:`AdvisorySuggestionDisplayReader` -- plugged into the canonical Workbench projection
  read scope. It reads only persisted advisory sets (no Odoo call, no engine run, no
  matching) and returns a hint only for a line that passes the display gate
  (:func:`app.advisory.suggestion_sets.display_block`). The Workbench projection itself
  stays pure: it receives ERP-neutral views and never ranks or checks anything.
* :class:`AdvisoryDisplayRefresher` -- the database-only display sweep. It asks the
  advisory tables which reviews may show something in Odoo that the gate no longer allows
  (or not yet show what it allows), and only then re-runs the canonical projection sync for
  those reviews. No due set means no Odoo call. A successful refresh is recorded with
  ``display_synced_at`` (never ``validated_at``); a failed one is retried with a bounded
  backoff, so failed clearing stays retryable and never extends validity.

Staleness contract: the Hub never *publishes* an expired suggestion (every projection,
from every flow, runs the gate with the 19-minute cutoff) and clears expired ones from
Odoo while the worker and Odoo are operational. During an outage the Odoo UI can keep
the last published suggestions until the next successful refresh.

Import boundary: this module may import ``app.advisory`` (allowlisted) but never another
``app.composition`` module, so ``app.composition.imports`` can compose the reader without
an import cycle (``app.composition.advisory`` imports ``imports``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy.orm import Session

from app.advisory.product_suggestions import SuggestionTier
from app.advisory.suggestion_sets import (
    DISPLAY_SWEEP_INTERVAL,
    DisplayedLine,
    StoredSuggestionSet,
    display_block,
)
from app.application.workbench.dto import ReviewStatus
from app.application.workbench.product_line_projection import ProductLineMatchState
from app.application.workbench.product_line_suggestion_view import ProductLineSuggestionView
from app.application.workbench.projection import WorkbenchProjection
from app.application.workbench.projection_sync_contracts import ProjectionSyncOutcome, ProjectionSyncResult
from app.persistence.product_line_suggestion_repository import SqlAlchemyProductLineSuggestionRepository

logger = logging.getLogger(__name__)

#: The F3 Studio UI shows this static warning next to the suggestions (not written by the Hub).
ADVISORY_UI_WARNING = (
    "Öneriler bilgilendirme amaçlıdır. Güncellikleri değişebilir; eşleştirme öncesinde ürünü doğrulayın."
)
TIER_LABELS: dict[SuggestionTier, str] = {
    SuggestionTier.T0_STALE_SUPPLIERINFO: "tedarikçi ürün kaydı artık mevcut; kontrollü yeniden sınıflandırma gerekir",
    SuggestionTier.T1_EXACT_IDENTIFIER: "kesin tanımlayıcı eşleşmesi",
    SuggestionTier.T2_PRIOR_MAPPING: "bu tedarikçi için daha önce eşleştirildi",
    SuggestionTier.T3_NAME_SIMILARITY: "ürün adı benzerliği",
}
#: Plain-text field; a longer summary is cut (the ranked lines always come first).
MAX_SUMMARY_LENGTH = 2000
#: At most this many reviews are refreshed per sweep; the rest wait for the next sweep.
MAX_REVIEWS_PER_SWEEP = 25
#: A failing review is retried after 60 s, doubling up to this cap (it is never dropped).
MAX_RETRY_BACKOFF = timedelta(minutes=5)

_REFRESHED_OUTCOMES = frozenset(
    {ProjectionSyncOutcome.CREATED, ProjectionSyncOutcome.UPDATED, ProjectionSyncOutcome.NO_CHANGE}
)


# ---------------------------------------------------------------------------------------- reader


class AdvisorySuggestionDisplayReader:
    """Gated hints for one projection, from the projection's own read-only session."""

    def __init__(self, session: Session, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._repository = SqlAlchemyProductLineSuggestionRepository(session)
        self._clock = clock

    def __call__(self, projection: WorkbenchProjection) -> tuple[ProductLineSuggestionView, ...]:
        lines = projection.product_lines or ()
        candidates = [line for line in lines if line.match_state is ProductLineMatchState.PRODUCT_NOT_FOUND]
        if projection.status is not ReviewStatus.PENDING_REVIEW or not candidates:
            return ()  # nothing can pass the gate: no advisory read at all
        now = self._clock()
        stored = self._repository.list_current_for_review(
            company_id=projection.company_id, review_id=projection.review_id
        )
        by_number: dict[str, list[StoredSuggestionSet]] = {}
        for item in stored:
            by_number.setdefault(item.line_number, []).append(item)
        views: list[ProductLineSuggestionView] = []
        blocked: dict[str, int] = {}
        for line in candidates:
            shown = DisplayedLine(
                review_pending=True,
                review_version=projection.version,
                line_key=line.line_key,
                line_number=line.line_number,
                product_not_found=True,
                supplier_partner_id=line.supplier_partner_id,
                seller_item_code=line.seller_item_code,
            )
            passing: list[StoredSuggestionSet] = []
            for item in by_number.get(line.line_number, ()):
                block = display_block(item, shown, now=now)
                if block is None:
                    passing.append(item)
                else:
                    blocked[block.value] = blocked.get(block.value, 0) + 1
            if len(passing) == 1:
                views.append(suggestion_view(passing[0]))
        if blocked:
            logger.info(
                "advisory.product_suggestions.display_blocked",
                extra={"review_id": projection.review_id, "version": projection.version, "blocked": blocked},
            )
        return tuple(views)


def suggestion_view(stored: StoredSuggestionSet) -> ProductLineSuggestionView:
    suggestions = stored.result.suggestions
    return ProductLineSuggestionView(
        line_key=stored.line_key,
        product_ids=tuple(suggestion.product_id for suggestion in suggestions),
        summary=render_summary(stored),
    )


def render_summary(stored: StoredSuggestionSet) -> str:
    """Deterministic plain text (no timestamps): ranked candidates, then de-duplicated warnings."""

    rows = [
        f"{suggestion.rank}. {stored.product_name(suggestion.product_id) or f'Ürün #{suggestion.product_id}'}"
        f" — {TIER_LABELS[suggestion.tier]} (skor {_score(suggestion.score)})"
        for suggestion in stored.result.suggestions
    ]
    warnings: list[str] = []
    for text in (
        *(warning for suggestion in stored.result.suggestions for warning in suggestion.warnings),
        *stored.result.notes,
    ):
        if isinstance(text, str) and text.strip() and text.strip() not in warnings:
            warnings.append(text.strip())
    summary = "\n".join([*rows, *(f"Uyarı: {warning}" for warning in warnings)])
    return summary if len(summary) <= MAX_SUMMARY_LENGTH else summary[: MAX_SUMMARY_LENGTH - 1] + "…"


def _score(value: Decimal) -> str:
    return f"{Decimal(value).quantize(Decimal('0.01'))}"


# ---------------------------------------------------------------------------------------- sweep


class ReviewSynchronize(Protocol):
    def __call__(self, *, review_id: str, company_id: int) -> ProjectionSyncResult: ...


@dataclass(frozen=True, slots=True)
class DisplaySweepResult:
    due_sets: int = 0
    due_reviews: int = 0
    refreshed: int = 0
    failed: int = 0
    marked_sets: int = 0
    #: Over the per-sweep cap; picked up by the next sweep.
    deferred: int = 0
    #: Still inside a failed review's retry backoff.
    backing_off: int = 0


class AdvisoryDisplayRefresher:
    """Database-only decision, then the canonical projection sync for due reviews only."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        company_id: int,
        synchronize: ReviewSynchronize,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        max_reviews: int = MAX_REVIEWS_PER_SWEEP,
        max_backoff: timedelta = MAX_RETRY_BACKOFF,
    ) -> None:
        self._session_factory = session_factory
        self._company_id = company_id
        self._synchronize = synchronize
        self._clock = clock
        self._monotonic = monotonic
        self._max_reviews = max_reviews
        self._max_backoff = max_backoff
        #: review id -> (consecutive failures, monotonic time of the next allowed attempt)
        self._retry: dict[str, tuple[int, float]] = {}

    def sweep(self) -> DisplaySweepResult:
        now = self._clock()
        with self._session() as session:
            due = SqlAlchemyProductLineSuggestionRepository(session).display_refresh_due(
                company_id=self._company_id, now=now
            )
        if not due:
            return DisplaySweepResult()
        by_review: dict[str, list[StoredSuggestionSet]] = {}
        for stored in due:  # ordered by set id: the oldest pending refresh first
            by_review.setdefault(stored.review_id, []).append(stored)
        refreshed = failed = marked = deferred = backing_off = attempted = 0
        for review_id, sets in by_review.items():
            if not self._may_retry(review_id):
                backing_off += 1
                continue
            if attempted >= self._max_reviews:
                deferred += 1
                continue
            attempted += 1
            started = self._clock()
            if self._refresh(review_id):
                marked += self._mark(sets, synced_at=started)
                self._retry.pop(review_id, None)
                refreshed += 1
            else:
                self._record_failure(review_id)
                failed += 1
        result = DisplaySweepResult(
            due_sets=len(due),
            due_reviews=len(by_review),
            refreshed=refreshed,
            failed=failed,
            marked_sets=marked,
            deferred=deferred,
            backing_off=backing_off,
        )
        logger.info(
            "advisory.product_suggestions.display_sweep",
            extra={"company_id": self._company_id, **{name: getattr(result, name) for name in result.__slots__}},
        )
        return result

    def _refresh(self, review_id: str) -> bool:
        try:
            result = self._synchronize(review_id=review_id, company_id=self._company_id)
        except Exception as exc:  # noqa: BLE001 - the synchronizer never raises; this is a last guard
            logger.warning(
                "advisory.product_suggestions.display_refresh_failed",
                extra={"review_id": review_id, "error_type": exc.__class__.__name__},
            )
            return False
        if result.outcome in _REFRESHED_OUTCOMES and not result.line_failed:
            return True
        logger.warning(
            "advisory.product_suggestions.display_refresh_failed",
            extra={
                "review_id": review_id,
                "outcome": result.outcome.value,
                "error": result.error or result.line_error,
            },
        )
        return False

    def _mark(self, sets: list[StoredSuggestionSet], *, synced_at: datetime) -> int:
        with self._session() as session:
            marked = SqlAlchemyProductLineSuggestionRepository(session).mark_display_synced(sets, synced_at=synced_at)
            session.commit()
            return marked

    def _may_retry(self, review_id: str) -> bool:
        entry = self._retry.get(review_id)
        return entry is None or self._monotonic() >= entry[1]

    def _record_failure(self, review_id: str) -> None:
        failures = self._retry.get(review_id, (0, 0.0))[0] + 1
        delay = min(DISPLAY_SWEEP_INTERVAL.total_seconds() * 2 ** (failures - 1), self._max_backoff.total_seconds())
        self._retry[review_id] = (failures, self._monotonic() + delay)

    @contextmanager
    def _session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()


__all__ = [
    "ADVISORY_UI_WARNING",
    "MAX_REVIEWS_PER_SWEEP",
    "TIER_LABELS",
    "AdvisoryDisplayRefresher",
    "AdvisorySuggestionDisplayReader",
    "DisplaySweepResult",
    "render_summary",
    "suggestion_view",
]
