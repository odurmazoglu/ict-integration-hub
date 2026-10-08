"""Reconcile the Odoo IPP Workbench projection with committed Hub state (OPS-UI-01A).

    python -m app.cli.reconcile_workbench_projection --company 1            # dry-run
    python -m app.cli.reconcile_workbench_projection --company 1 --apply    # create/update rows

Dry-run is the default and performs zero Odoo writes and zero Hub writes: it lists
every Hub review of the company, diffs its full projection snapshot against the
Odoo Workbench row and reports CREATE / UPDATE / NO_CHANGE / SKIPPED_STALE / ERROR
with field-level differences.

PR B: when ``ODOO_WORKBENCH_PRODUCT_LINE_PROJECTION_ENABLED`` is true, each review's
product line child rows (``x_ipp_wb_product_line``) are diffed too and reported as
``line CREATE / UPDATE / DEACTIVATE`` (``NO_CHANGE`` lines are counted, not listed),
with separate ``Lines:`` totals. A product line error never hides the parent result
and makes the exit code 1. Disabled, the output is exactly the parent-only report.

``--apply`` creates/updates Workbench projection rows only, through the same
canonical :class:`WorkbenchProjectionSynchronizer` every runtime transition uses.
It never modifies Hub state (the Hub session is opened read-only), never executes
or replays decisions, never creates authorizations or Vendor Bills, and never
touches account.move, partners, products, categories, supplierinfo or Studio
metadata. It does not depend on, or change, ``ODOO_WORKBENCH_PROJECTION_PUBLISH_ENABLED``.

One review's error never stops the run; the exit code is 1 when any review errored
and 2 for a configuration error (e.g. an incomplete Workbench field mapping).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, TextIO

from app.application.workbench.dto import ReviewQueueResult, ReviewStatus
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workbench.projection_sync_contracts import (
    ProductLineSyncOutcome,
    ProjectionSyncOutcome,
    ProjectionSyncResult,
)
from app.application.workbench.queries import MAX_REVIEW_QUEUE_LIMIT, ReviewQueueQuery

_LABELS = {
    ProjectionSyncOutcome.CREATED: "CREATE",
    ProjectionSyncOutcome.UPDATED: "UPDATE",
    ProjectionSyncOutcome.NO_CHANGE: "NO_CHANGE",
    ProjectionSyncOutcome.SKIPPED_STALE: "SKIPPED_STALE",
    ProjectionSyncOutcome.ERROR: "ERROR",
}
_LINE_LABELS = {
    ProductLineSyncOutcome.CREATED: "CREATE",
    ProductLineSyncOutcome.UPDATED: "UPDATE",
    ProductLineSyncOutcome.NO_CHANGE: "NO_CHANGE",
    ProductLineSyncOutcome.DEACTIVATED: "DEACTIVATE",
}
_VALUE_PREVIEW = 160
EXIT_REVIEW_ERRORS = 1
EXIT_CONFIGURATION_ERROR = 2


class _Planner(Protocol):
    def plan(self, *, review_id: str, company_id: int) -> ProjectionSyncResult:
        pass

    def sync(self, *, review_id: str, company_id: int) -> ProjectionSyncResult:
        pass


class _ReviewLister(Protocol):
    def list_review_items(self, query: ReviewQueueQuery) -> ReviewQueueResult:
        pass


@dataclass(slots=True)
class ReconcileReport:
    apply: bool
    results: list[ProjectionSyncResult] = field(default_factory=list)

    @property
    def totals(self) -> Counter[str]:
        return Counter(_LABELS[result.outcome] for result in self.results)

    @property
    def line_totals(self) -> Counter[str]:
        totals = Counter(_LINE_LABELS[line.outcome] for result in self.results for line in result.line_results)
        totals["ERROR"] = sum(1 for result in self.results if result.line_failed)
        return totals

    @property
    def has_line_projection(self) -> bool:
        return any(result.line_results or result.line_failed for result in self.results)

    @property
    def has_errors(self) -> bool:
        return any(result.failed or result.line_failed for result in self.results)


def list_review_ids(lister: _ReviewLister, *, company_id: int) -> tuple[str, ...]:
    """Every Hub review of the company, across all statuses, in a stable order."""

    review_ids: list[str] = []
    for status in ReviewStatus:
        offset = 0
        while True:
            page = lister.list_review_items(
                ReviewQueueQuery(company_id=company_id, status=status, limit=MAX_REVIEW_QUEUE_LIMIT, offset=offset)
            )
            review_ids.extend(item.review_id for item in page.items)
            offset += len(page.items)
            if not page.items or offset >= page.total_count:
                break
    return tuple(dict.fromkeys(review_ids))


def run_reconcile(
    synchronizer: _Planner,
    *,
    review_ids: Iterable[str],
    company_id: int,
    apply: bool,
    out: TextIO,
) -> ReconcileReport:
    report = ReconcileReport(apply=apply)
    mode = "APPLY (Workbench projection rows only)" if apply else "DRY-RUN (no Odoo or Hub writes)"
    out.write(f"Workbench projection reconcile | company {company_id} | {mode}\n")
    for review_id in review_ids:
        step = synchronizer.sync if apply else synchronizer.plan
        result = step(review_id=review_id, company_id=company_id)
        report.results.append(result)
        _write_result(out, result)
    totals = report.totals
    summary = " ".join(f"{label}={totals.get(label, 0)}" for label in _LABELS.values())
    out.write(f"Totals: {summary} | reviews={len(report.results)} | applied={apply}\n")
    if report.has_line_projection:
        line_totals = report.line_totals
        line_summary = " ".join(f"{label}={line_totals.get(label, 0)}" for label in (*_LINE_LABELS.values(), "ERROR"))
        out.write(f"Lines: {line_summary} | applied={apply}\n")
    return report


def _write_result(out: TextIO, result: ProjectionSyncResult) -> None:
    record = f" odoo_id={result.odoo_record_id}" if result.odoo_record_id is not None else ""
    version = f" v{result.review_version}" if result.review_version is not None else ""
    applied = " (applied)" if result.applied else ""
    out.write(f"{_LABELS[result.outcome]:<13} {result.review_id}{version}{record}{applied}\n")
    if result.error:
        out.write(f"    reason: {result.error}\n")
    for change in result.changes:
        out.write(f"    {change.field}: {_preview(change.before)} -> {_preview(change.after)}\n")
    if result.line_error:
        out.write(f"    line ERROR: {result.line_error}\n")
    for line in result.line_results:
        if line.outcome is ProductLineSyncOutcome.NO_CHANGE:
            continue
        line_record = f" odoo_id={line.odoo_record_id}" if line.odoo_record_id is not None else ""
        out.write(f"    line {_LINE_LABELS[line.outcome]:<10} {line.line_number or '?'} {line.line_key}{line_record}\n")
        for change in line.changes:
            out.write(f"        {change.field}: {_preview(change.before)} -> {_preview(change.after)}\n")


def _preview(value: object) -> str:
    text = repr(value)
    return text if len(text) <= _VALUE_PREVIEW else text[: _VALUE_PREVIEW - 3] + "..."


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.reconcile_workbench_projection",
        description="Reconcile Odoo IPP Workbench projection rows with committed Hub review state.",
    )
    parser.add_argument("--company", type=int, required=True, help="Hub/Odoo company id (e.g. 1).")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create/update Workbench projection rows. Without it the run is a zero-write dry-run.",
    )
    parser.add_argument(
        "--review-id",
        action="append",
        default=None,
        help="Limit the run to this review id (repeatable). Default: every review of the company.",
    )
    args = parser.parse_args(argv)
    if args.company <= 0:
        parser.error("--company must be a positive id.")
    return args


def main(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO | None = None,
    engine: object | None = None,
) -> int:
    args = _parse_args(argv)
    stream = out or sys.stdout
    # Imported lazily so ``--help`` and argument errors never need a database or Odoo.
    from app.composition.imports import build_workbench_projection_synchronizer, open_read_only_session
    from app.core.config import get_settings
    from app.persistence import SqlAlchemyReviewRepository

    if engine is None:
        from app.db.session import engine as default_engine

        engine = default_engine
    try:
        # Each review is projected through its own private read-only session.
        synchronizer = build_workbench_projection_synchronizer(engine=engine, settings=get_settings())
    except WorkbenchContractError as exc:
        # e.g. an incomplete ODOO_WORKBENCH_PUBLISHER_* field mapping: nothing was read or written.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIGURATION_ERROR
    if args.review_id:
        review_ids: tuple[str, ...] = tuple(dict.fromkeys(args.review_id))
    else:
        # The CLI never writes Hub state: listing uses its own read-only session too.
        with open_read_only_session(engine) as read_session:
            review_ids = list_review_ids(SqlAlchemyReviewRepository(read_session), company_id=args.company)
    report = run_reconcile(synchronizer, review_ids=review_ids, company_id=args.company, apply=args.apply, out=stream)
    return EXIT_REVIEW_ERRORS if report.has_errors else 0


if __name__ == "__main__":  # pragma: no cover - thin process entry point
    raise SystemExit(main())
