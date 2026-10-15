"""PR F / F2: composition of the independent advisory product-suggestion worker (ADR-0014).

Wires Hub-only line and history sources, the bounded read-only Odoo catalog reader, the
advisory repository and a database single-flight lock around one tick. Imported only by
the Hub worker and the advisory CLI -- never by classification, matching, decisions,
execution, operator requests or accounting.

F3.1: with the display flag on, every tick ends with the database-only display refresh
(post-tick refresh), and an independent 60 s sweep task runs the same refresh under the
same lock; see :mod:`app.composition.advisory_display`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.advisory.product_suggestions import PriorMapping, SuggestionPolicy
from app.advisory.suggestion_sets import DISPLAY_SWEEP_INTERVAL, AdvisoryLine
from app.application.workbench.dto import ReviewItem, ReviewStatus, review_reasons_role
from app.application.workbench.exceptions import ReviewNotFoundError
from app.application.workbench.product_line_projection import (
    ProductLineFacts,
    ProductLineMatchState,
    build_product_line_projections,
)
from app.application.workbench.queries import MAX_REVIEW_QUEUE_LIMIT, ReviewQueueQuery
from app.composition.advisory_display import AdvisoryDisplayRefresher, DisplaySweepResult
from app.composition.imports import _product_line_read_facts, build_workbench_projection_synchronizer
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.advisory_catalog_reader import OdooAdvisoryCatalogReader
from app.erp.odoo.workbench_product_line_publisher import OdooWorkbenchProductLineFieldMapping
from app.erp.odoo.workbench_projection_publisher import (
    OdooWorkbenchJson2ProjectionAdapter,
    OdooWorkbenchProjectionAdapter,
)
from app.erp.odoo.workbench_suggestion_field_cleaner import (
    OdooWorkbenchSuggestionFieldCleaner,
    PopulatedSuggestionRow,
)
from app.matching import PartnerMatchStatus, ProductMatchStatus
from app.matching.product import MATCHED_BY_SUPPLIER_PRODUCT_CODE
from app.models.workbench_operator_request import WorkbenchOperatorRequest
from app.models.workbench_product_line_creation import WorkbenchProductLineCreation
from app.persistence import SqlAlchemyReviewRepository, SqlAlchemyReviewSourceInvoiceEvidenceReader
from app.persistence.product_line_suggestion_repository import SqlAlchemyProductLineSuggestionRepository
from app.services.advisory_product_suggestions import (
    AdvisoryCatalogReader,
    AdvisoryProductSuggestionService,
    AdvisoryTickResult,
    HubLineSnapshot,
)
from app.services.uyumsoft_inbound_poll import InProcessPollLock, PollLock, PostgresAdvisoryPollLock

logger = logging.getLogger(__name__)

#: Distinct from the Uyumsoft poll and operator request locks: no tick ever blocks another.
ADVISORY_SUGGESTIONS_LOCK_KEY = -2752363236075536946
#: Ledger actions whose completion mapped a supplier code to an existing product.
MAPPING_LEDGER_ACTIONS = ("product_mapping", "product_line_mapping")


class HubAdvisoryLineSource:
    """Current PRODUCT_NOT_FOUND lines of pending reviews, exactly as the child projection derives them.

    Committed Hub rows only (no Odoo call). The supplier is the one the persisted product
    matching of the current version ran under -- the effective commercial supplier.
    """

    def __init__(self, session: Session) -> None:
        self._reviews = SqlAlchemyReviewRepository(session)
        self._sources = SqlAlchemyReviewSourceInvoiceEvidenceReader(session)

    def snapshot(self, *, company_id: int) -> HubLineSnapshot:
        reviews = self._pending_reviews(company_id)
        lines: list[AdvisoryLine] = []
        for review in sorted(reviews, key=lambda item: item.review_id):
            try:
                lines.extend(self._lines(review, company_id))
            except Exception as exc:  # noqa: BLE001 - one review never stops the advisory tick
                logger.warning(
                    "advisory.product_suggestions.review_skipped",
                    extra={"review_id": review.review_id, "error_type": exc.__class__.__name__},
                )
        return HubLineSnapshot(lines=tuple(lines), pending_versions={r.review_id: r.version for r in reviews})

    def _pending_reviews(self, company_id: int) -> list[ReviewItem]:
        reviews: list[ReviewItem] = []
        offset = 0
        while True:
            page = self._reviews.list_review_items(
                ReviewQueueQuery(
                    company_id=company_id,
                    status=ReviewStatus.PENDING_REVIEW,
                    limit=MAX_REVIEW_QUEUE_LIMIT,
                    offset=offset,
                )
            )
            reviews.extend(page.items)
            offset += len(page.items)
            if not page.items or offset >= page.total_count:
                return reviews

    def _lines(self, review: ReviewItem, company_id: int) -> list[AdvisoryLine]:
        read = _product_line_read_facts(
            review, company_id, review.version, source_reader=self._sources, evidence_reader=self._reviews
        )
        facts = ProductLineFacts(
            review_id=review.review_id,
            company_id=company_id,
            review_version=review.version,
            invoice_number=review.invoice_number,
            reasons=review.review_reasons,
            reasons_role=review_reasons_role(review.status),
            source_lines=read.source_lines,
            supplier_match=read.supplier_match,
            evidence_lines=read.evidence_lines,
            had_product_reasons=read.had_product_reasons,
        )
        manufacturer_codes = {
            (line.line_number or "").strip(): line.manufacturer_item_code for line in read.source_lines
        }
        return [
            AdvisoryLine(
                company_id=company_id,
                review_id=review.review_id,
                review_version=review.version,
                line_number=projection.line_number,
                line_key=projection.line_key,
                line_sequence=projection.line_sequence,
                supplier_partner_id=projection.supplier_partner_id,
                seller_item_code=projection.seller_item_code,
                description=projection.description,
                manufacturer_item_code=manufacturer_codes.get(projection.line_number),
            )
            for projection in build_product_line_projections(facts)
            if projection.match_state is ProductLineMatchState.PRODUCT_NOT_FOUND
            and projection.supplier_partner_id is not None
        ]


class HubPriorMappingSource:
    """T2 history from the Hub's own completed mappings (no decision-based history in F2).

    * a completed new-product creation: its supplier, seller code, invoice description and product;
    * a completed product-mapping ledger entry at version ``V``: the lines that were
      NOT_FOUND at ``V`` and are matched by supplier code at ``V + 1`` under the same
      matched supplier -- read from persisted execution evidence, never from free text.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._reviews = SqlAlchemyReviewRepository(session)
        self._sources = SqlAlchemyReviewSourceInvoiceEvidenceReader(session)

    def prior_mappings(self, *, company_id: int) -> tuple[PriorMapping, ...]:
        return (*self._creations(company_id), *self._ledger_mappings(company_id))

    def _creations(self, company_id: int) -> list[PriorMapping]:
        rows = self._session.scalars(
            select(WorkbenchProductLineCreation)
            .where(
                WorkbenchProductLineCreation.company_id == company_id,
                WorkbenchProductLineCreation.status == "completed",
                WorkbenchProductLineCreation.product_id.is_not(None),
            )
            .order_by(WorkbenchProductLineCreation.id)
        )
        return [
            PriorMapping(
                supplier_partner_id=row.supplier_partner_id,
                product_id=int(row.product_id),
                seller_item_code=row.seller_item_code,
                description=row.supplier_product_name,
                source=f"ürün oluşturma #{row.id}",
            )
            for row in rows
            if row.product_id is not None
        ]

    def _ledger_mappings(self, company_id: int) -> list[PriorMapping]:
        rows = self._session.scalars(
            select(WorkbenchOperatorRequest)
            .where(
                WorkbenchOperatorRequest.company_id == company_id,
                WorkbenchOperatorRequest.action.in_(MAPPING_LEDGER_ACTIONS),
                WorkbenchOperatorRequest.status == "completed",
            )
            .order_by(WorkbenchOperatorRequest.id)
        )
        mappings: list[PriorMapping] = []
        for row in rows:
            try:
                mappings.extend(self._mapped_lines(row, company_id))
            except ReviewNotFoundError:
                continue
        return mappings

    def _mapped_lines(self, row: WorkbenchOperatorRequest, company_id: int) -> list[PriorMapping]:
        before = self._reviews.get_review_execution_evidence(
            review_id=row.review_id, company_id=company_id, review_version=row.expected_version
        )
        after = self._reviews.get_review_execution_evidence(
            review_id=row.review_id, company_id=company_id, review_version=row.expected_version + 1
        )
        partner = after.partner_match
        if partner.status is not PartnerMatchStatus.MATCHED or partner.partner_id is None:
            return []
        if before.partner_match.partner_id != partner.partner_id:
            return []
        not_found_before = {
            (item.line_number or "").strip()
            for item in before.product_match.line_results
            if item.result.status is ProductMatchStatus.NOT_FOUND
        }
        lines = {(line.line_number or "").strip(): line for line in after.invoice.lines}
        found: list[PriorMapping] = []
        for item in after.product_match.line_results:
            number = (item.line_number or "").strip()
            result = item.result
            if (
                number in not_found_before
                and result.status is ProductMatchStatus.MATCHED
                and result.matched_by == MATCHED_BY_SUPPLIER_PRODUCT_CODE
                and result.product_id is not None
                and number in lines
            ):
                found.append(
                    PriorMapping(
                        supplier_partner_id=partner.partner_id,
                        product_id=result.product_id,
                        seller_item_code=lines[number].seller_item_code,
                        description=lines[number].description,
                        source=f"eşleştirme talebi #{row.id}",
                    )
                )
        return found


class AdvisoryTick:
    """One single-flight advisory tick on its own session; a held lock means skip."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        lock: PollLock,
        company_id: int,
        catalog_reader_factory: Callable[[], AdvisoryCatalogReader],
        policy: SuggestionPolicy | None = None,
        display_refresher: AdvisoryDisplayRefresher | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._lock = lock
        self._company_id = company_id
        self._catalog_reader_factory = catalog_reader_factory
        self._policy = policy
        self._display_refresher = display_refresher

    def run(self) -> AdvisoryTickResult | None:
        with self._lock.hold() as acquired:
            if not acquired:
                logger.info("advisory.product_suggestions.skipped_locked")
                return None
            with self._session() as session:
                result = advisory_service(
                    session, catalog_reader=self._catalog_reader_factory(), policy=self._policy
                ).run(company_id=self._company_id)
            if self._display_refresher is not None:
                # F3.1 post-tick refresh: publish new sets and clear retired ones right away.
                try:
                    self._display_refresher.sweep()
                except Exception as exc:  # noqa: BLE001 - the 60 s sweep retries; the tick result stands
                    logger.warning(
                        "advisory.product_suggestions.display_refresh_crashed",
                        extra={"error_type": exc.__class__.__name__},
                    )
            return result

    @contextmanager
    def _session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()


class AdvisoryDisplaySweep:
    """F3.1: the 60 s database-only display sweep, single-flight with the advisory tick."""

    def __init__(self, *, lock: PollLock, refresher: AdvisoryDisplayRefresher) -> None:
        self._lock = lock
        self._refresher = refresher

    def run(self) -> DisplaySweepResult | None:
        with self._lock.hold() as acquired:
            if not acquired:
                logger.info("advisory.product_suggestions.display_sweep_skipped_locked")
                return None
            return self._refresher.sweep()


def advisory_service(
    session: Session, *, catalog_reader: AdvisoryCatalogReader, policy: SuggestionPolicy | None
) -> AdvisoryProductSuggestionService:
    return AdvisoryProductSuggestionService(
        line_source=HubAdvisoryLineSource(session),
        prior_source=HubPriorMappingSource(session),
        catalog_reader=catalog_reader,
        repository=SqlAlchemyProductLineSuggestionRepository(session),
        commit=session.commit,
        rollback=session.rollback,
        policy=policy,
    )


def advisory_policy(settings: Settings) -> SuggestionPolicy:
    """The same manual-only (variant catalog) supplier and category roots as PR D creation."""

    return SuggestionPolicy(
        manual_only_supplier_ids=frozenset(settings.product_creation_manual_only_supplier_ids),
        manual_only_category_ids=frozenset(settings.product_creation_manual_only_category_ids),
    )


def odoo_catalog_reader(settings: Settings, odoo_client: OdooJson2Client | None = None) -> OdooAdvisoryCatalogReader:
    return OdooAdvisoryCatalogReader(
        adapter=OdooReadOnlyAdapter(client=odoo_client or OdooJson2Client.from_settings(settings))
    )


def advisory_lock(engine: Engine) -> PollLock:
    return (
        PostgresAdvisoryPollLock(engine, key=ADVISORY_SUGGESTIONS_LOCK_KEY)
        if engine.dialect.name == "postgresql"
        else InProcessPollLock()
    )


def build_advisory_tick(
    *, settings: Settings, engine: Engine, odoo_client: OdooJson2Client | None = None
) -> AdvisoryTick:
    if settings.odoo_workbench_operator_requests_company_id is None:
        raise ValueError("ODOO_WORKBENCH_OPERATOR_REQUESTS_COMPANY_ID is required.")
    return AdvisoryTick(
        session_factory=sessionmaker(bind=engine, autocommit=False, autoflush=False),
        lock=advisory_lock(engine),
        company_id=settings.odoo_workbench_operator_requests_company_id,
        catalog_reader_factory=lambda: odoo_catalog_reader(settings, odoo_client),
        policy=advisory_policy(settings),
        display_refresher=(
            build_advisory_display_refresher(settings=settings, engine=engine, odoo_client=odoo_client)
            if settings.odoo_workbench_product_line_suggestions_display_enabled
            else None
        ),
    )


def build_advisory_display_refresher(
    *, settings: Settings, engine: Engine, odoo_client: OdooJson2Client | None = None
) -> AdvisoryDisplayRefresher:
    """The canonical projection synchronizer (display on) behind the database-only due check."""

    if settings.odoo_workbench_operator_requests_company_id is None:
        raise ValueError("ODOO_WORKBENCH_OPERATOR_REQUESTS_COMPANY_ID is required.")
    if not settings.odoo_workbench_product_line_suggestions_display_enabled:
        raise ValueError("ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTIONS_DISPLAY_ENABLED is off.")
    synchronizer = build_workbench_projection_synchronizer(engine=engine, settings=settings, odoo_client=odoo_client)
    return AdvisoryDisplayRefresher(
        session_factory=sessionmaker(bind=engine, autocommit=False, autoflush=False),
        company_id=settings.odoo_workbench_operator_requests_company_id,
        synchronize=synchronizer.sync,
    )


# ---------------------------------------------------------------------------------------- clear (F3.6b)

#: Every Odoo read/write of ``clear-display`` is tried this often; the wait doubles from the backoff.
CLEAR_ATTEMPTS = 3
CLEAR_RETRY_BACKOFF = timedelta(seconds=5)
CLEAR_MAX_BACKOFF = timedelta(seconds=60)
#: Wait outside the lock after the markers are reset: a display-enabled worker would re-mark (and
#: republish) a due set within one sweep interval, so two intervals plus margin expose it.
CLEAR_SETTLE = 2 * DISPLAY_SWEEP_INTERVAL + timedelta(seconds=30)
#: The advisory tick holds the shared lock for one catalog read and is retried this often.
CLEAR_LOCK_ATTEMPTS = 20
CLEAR_LOCK_WAIT = timedelta(seconds=3)


class DisplayClearOutcome(StrEnum):
    DRY_RUN = "dry_run"
    CLEARED = "cleared"
    LOCK_BUSY = "lock_busy"
    READ_FAILED = "read_failed"
    INCOMPLETE = "incomplete"
    REPOPULATED = "repopulated"


@dataclass(frozen=True, slots=True)
class DisplayClearReport:
    outcome: DisplayClearOutcome
    discovered: tuple[PopulatedSuggestionRow, ...] = ()
    cleared: tuple[int, ...] = ()
    failed: tuple[int, ...] = ()
    remaining: tuple[PopulatedSuggestionRow, ...] = ()
    markers_reset: int = 0
    markers_after_settle: int = 0
    error: str | None = None

    @property
    def complete(self) -> bool:
        """Only a verified, settled clear: Odoo empty and no display-enabled process re-marked a set."""

        return self.outcome is DisplayClearOutcome.CLEARED


class SuggestionFieldCleaner(Protocol):
    def populated_rows(self, *, company_id: int) -> tuple[PopulatedSuggestionRow, ...]: ...

    def clear(self, row: PopulatedSuggestionRow) -> None: ...


class AdvisoryDisplayCleaner:
    """Explicit display rollback: clear both suggestion fields in Odoo, verify, then re-arm the sweep.

    Runs only with display OFF (checked by the CLI). Inside the advisory lock shared with the
    generation tick and the display sweep it clears every populated row (bounded retries),
    re-reads Odoo, and only when nothing is left resets the display markers of the current sets
    (so re-enabling display republishes them once). It then waits ``settle`` outside the lock and
    confirms under the lock that Odoo is still empty and no set was re-marked -- only a
    display-enabled refresh marks a set, so a worker still running with display ON fails the run
    instead of letting it report success. Nothing is reset when the clear is incomplete.
    """

    def __init__(
        self,
        *,
        cleaner: SuggestionFieldCleaner,
        session_factory: Callable[[], Session],
        lock: PollLock,
        company_id: int,
        sleep: Callable[[float], None] = time.sleep,
        attempts: int = CLEAR_ATTEMPTS,
        backoff: timedelta = CLEAR_RETRY_BACKOFF,
        max_backoff: timedelta = CLEAR_MAX_BACKOFF,
        settle: timedelta = CLEAR_SETTLE,
        lock_attempts: int = CLEAR_LOCK_ATTEMPTS,
        lock_wait: timedelta = CLEAR_LOCK_WAIT,
    ) -> None:
        if attempts < 1 or lock_attempts < 1:
            raise ValueError("attempts and lock_attempts must be positive.")
        self._cleaner = cleaner
        self._session_factory = session_factory
        self._lock = lock
        self._company_id = company_id
        self._sleep = sleep
        self._attempts = attempts
        self._backoff = backoff
        self._max_backoff = max_backoff
        self._settle = settle
        self._lock_attempts = lock_attempts
        self._lock_wait = lock_wait

    def preview(self) -> DisplayClearReport:
        """Dry-run: what ``apply`` would clear. No lock, no Odoo or Hub write."""

        try:
            rows = self._read()
        except ErpRepositoryError as exc:
            return DisplayClearReport(outcome=DisplayClearOutcome.READ_FAILED, error=_safe_error(exc))
        return DisplayClearReport(outcome=DisplayClearOutcome.DRY_RUN, discovered=rows, remaining=rows)

    def apply(self) -> DisplayClearReport:
        first = self._locked(self._clear_and_reset)
        if first is None:
            return self._finish(DisplayClearReport(outcome=DisplayClearOutcome.LOCK_BUSY, error=_LOCK_BUSY))
        if not first.complete:
            return self._finish(first)
        self._sleep(self._settle.total_seconds())
        confirmed = self._locked(lambda: self._confirm(first))
        if confirmed is None:
            return self._finish(replace(first, outcome=DisplayClearOutcome.LOCK_BUSY, error=_LOCK_BUSY_CONFIRM))
        return self._finish(confirmed)

    # ------------------------------------------------------------------ steps (inside the lock)

    def _clear_and_reset(self) -> DisplayClearReport:
        try:
            discovered = self._read()
        except ErpRepositoryError as exc:
            return DisplayClearReport(outcome=DisplayClearOutcome.READ_FAILED, error=_safe_error(exc))
        cleared: list[int] = []
        failed: list[int] = []
        for row in discovered:
            (cleared if self._clear_row(row) else failed).append(row.record_id)
        report = DisplayClearReport(
            outcome=DisplayClearOutcome.INCOMPLETE, discovered=discovered, cleared=tuple(cleared), failed=tuple(failed)
        )
        try:
            remaining = self._read()
        except ErpRepositoryError as exc:
            return replace(report, error=f"verification read failed: {_safe_error(exc)}")
        if remaining or failed:
            return replace(report, remaining=remaining)
        return replace(report, outcome=DisplayClearOutcome.CLEARED, markers_reset=self._reset_markers())

    def _confirm(self, first: DisplayClearReport) -> DisplayClearReport:
        marked = self._count_markers()
        try:
            remaining = self._read()
        except ErpRepositoryError as exc:
            error = f"confirmation read failed: {_safe_error(exc)}"
            return replace(first, outcome=DisplayClearOutcome.INCOMPLETE, error=error)
        if marked or remaining:
            return replace(
                first,
                outcome=DisplayClearOutcome.REPOPULATED,
                remaining=remaining,
                markers_after_settle=marked,
                error="a display-enabled process re-synchronized suggestions during the settle window",
            )
        return first

    # ------------------------------------------------------------------ Odoo (bounded retries)

    def _read(self) -> tuple[PopulatedSuggestionRow, ...]:
        for attempt in range(1, self._attempts + 1):
            try:
                return self._cleaner.populated_rows(company_id=self._company_id)
            except ErpRepositoryError as exc:
                logger.warning(
                    "advisory.product_suggestions.display_clear_read_failed",
                    extra={"company_id": self._company_id, "attempt": attempt, "error": _safe_error(exc)},
                )
                if attempt == self._attempts:
                    raise
                self._sleep(self._delay(attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    def _clear_row(self, row: PopulatedSuggestionRow) -> bool:
        for attempt in range(1, self._attempts + 1):
            try:
                self._cleaner.clear(row)
            except ErpRepositoryError as exc:
                logger.warning(
                    "advisory.product_suggestions.display_clear_write_failed",
                    extra={"record_id": row.record_id, "review_id": row.review_id, "attempt": attempt,
                           "error": _safe_error(exc)},
                )  # fmt: skip
                if attempt < self._attempts:
                    self._sleep(self._delay(attempt))
                continue
            logger.info(
                "advisory.product_suggestions.display_cleared_row",
                extra={"record_id": row.record_id, "review_id": row.review_id, "line_key": row.line_key,
                       "products": list(row.product_ids), "summary_digest": row.summary_digest, "attempt": attempt},
            )  # fmt: skip
            return True
        return False

    def _delay(self, attempt: int) -> float:
        return min(self._backoff.total_seconds() * 2 ** (attempt - 1), self._max_backoff.total_seconds())

    # ------------------------------------------------------------------ Hub (compare-and-set markers)

    def _reset_markers(self) -> int:
        with self._session() as session:
            repository = SqlAlchemyProductLineSuggestionRepository(session)
            reset = repository.reset_display_markers(company_id=self._company_id)
            session.commit()
            return reset

    def _count_markers(self) -> int:
        with self._session() as session:
            return SqlAlchemyProductLineSuggestionRepository(session).count_display_marked_current(
                company_id=self._company_id
            )

    # ------------------------------------------------------------------ plumbing

    def _locked(self, step: Callable[[], DisplayClearReport]) -> DisplayClearReport | None:
        for attempt in range(1, self._lock_attempts + 1):
            with self._lock.hold() as acquired:
                if acquired:
                    return step()
            logger.info(
                "advisory.product_suggestions.display_clear_lock_busy",
                extra={"company_id": self._company_id, "attempt": attempt},
            )
            if attempt < self._lock_attempts:
                self._sleep(self._lock_wait.total_seconds())
        return None

    def _finish(self, report: DisplayClearReport) -> DisplayClearReport:
        log = logger.info if report.complete else logger.warning
        log(
            "advisory.product_suggestions.display_clear_finished",
            extra={
                "company_id": self._company_id,
                "outcome": report.outcome.value,
                "discovered": len(report.discovered),
                "cleared": len(report.cleared),
                "failed": len(report.failed),
                "remaining": len(report.remaining),
                "markers_reset": report.markers_reset,
                "markers_after_settle": report.markers_after_settle,
            },
        )
        return report

    @contextmanager
    def _session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()


_LOCK_BUSY = "the advisory lock stayed busy (tick or display sweep); nothing was written"
_LOCK_BUSY_CONFIRM = "Odoo was cleared but the advisory lock stayed busy, so the settle check could not confirm it"


def _safe_error(exc: Exception) -> str:
    return getattr(exc, "safe_message", None) or exc.__class__.__name__


def build_advisory_display_cleaner(
    *,
    settings: Settings,
    engine: Engine,
    company_id: int,
    adapter: OdooWorkbenchProjectionAdapter | None = None,
    mapping: OdooWorkbenchProductLineFieldMapping | None = None,
    **options: Any,
) -> AdvisoryDisplayCleaner:
    """F3.6b ``clear-display``: only with display OFF and both suggestion fields mapped."""

    if settings.odoo_workbench_product_line_suggestions_display_enabled:
        raise ValueError(
            "ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTIONS_DISPLAY_ENABLED is on: turn display off (and recreate "
            "the API and worker) before clearing, or the display sweep republishes the suggestions."
        )
    resolved_mapping = mapping or OdooWorkbenchProductLineFieldMapping.from_environment()
    cleaner = OdooWorkbenchSuggestionFieldCleaner(
        adapter=adapter or OdooWorkbenchJson2ProjectionAdapter.from_settings(settings), mapping=resolved_mapping
    )
    return AdvisoryDisplayCleaner(
        cleaner=cleaner,
        session_factory=sessionmaker(bind=engine, autocommit=False, autoflush=False),
        lock=advisory_lock(engine),
        company_id=company_id,
        **options,
    )


def build_advisory_display_sweep(
    *, settings: Settings, engine: Engine, odoo_client: OdooJson2Client | None = None
) -> AdvisoryDisplaySweep:
    return AdvisoryDisplaySweep(
        lock=advisory_lock(engine),
        refresher=build_advisory_display_refresher(settings=settings, engine=engine, odoo_client=odoo_client),
    )


__all__ = [
    "ADVISORY_SUGGESTIONS_LOCK_KEY",
    "CLEAR_SETTLE",
    "AdvisoryDisplayCleaner",
    "AdvisoryDisplaySweep",
    "AdvisoryTick",
    "DisplayClearOutcome",
    "DisplayClearReport",
    "HubAdvisoryLineSource",
    "HubPriorMappingSource",
    "advisory_lock",
    "advisory_policy",
    "advisory_service",
    "build_advisory_display_cleaner",
    "build_advisory_display_refresher",
    "build_advisory_display_sweep",
    "build_advisory_tick",
    "odoo_catalog_reader",
]
