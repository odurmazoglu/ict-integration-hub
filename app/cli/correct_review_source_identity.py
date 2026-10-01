"""Audited append-only correction of a review's source-invoice identity.

    python -m app.cli.correct_review_source_identity --company 1 \\
        --review 'review:<uuid>@<expected_version>' [--review ...] \\
        --approved-by <operator> [--apply]

Dry-run is the default: every review is evaluated in its own PostgreSQL READ ONLY
session, Odoo is only read (deterministic matching, current Workbench row), and
nothing is written anywhere. The report lists every precondition, the old/new
value, the version step, the expected Hub writes and the expected Workbench
projection changes.

``--apply`` processes only the explicitly listed reviews, each in its own
transaction (one review's refusal or error never affects another). A committed
correction is followed by the normal post-commit Workbench projection sync; a
projection failure is reported but never undoes the Hub correction (repair it with
``app.cli.reconcile_workbench_projection``). Re-applying an already-corrected review
reports ALREADY_APPLIED and writes nothing.

It never writes res.partner, account.move, decisions, authorizations, executions,
Vendor Bills or Studio metadata. Exit codes: 0 all reviews WOULD_APPLY / APPLIED /
ALREADY_APPLIED / NO_CHANGE; 1 any review REFUSED or ERROR; 2 configuration error.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol, TextIO

from app.application.workbench.projection_sync_contracts import ProjectionSyncResult
from app.application.workbench.source_identity_correction import (
    CorrectReviewSourceIdentityCommand,
    SourceIdentityCorrectionOutcome,
    SourceIdentityCorrectionReport,
    SourceInvoiceCorrectionField,
    SourceInvoiceCorrectionReason,
)

EXIT_REVIEW_FAILURES = 1
EXIT_CONFIGURATION_ERROR = 2
_SUCCESS_OUTCOMES = frozenset(
    {
        SourceIdentityCorrectionOutcome.WOULD_APPLY.value,
        SourceIdentityCorrectionOutcome.APPLIED.value,
        SourceIdentityCorrectionOutcome.ALREADY_APPLIED.value,
        SourceIdentityCorrectionOutcome.NO_CHANGE.value,
    }
)


class _UseCase(Protocol):
    async def execute(self, command: CorrectReviewSourceIdentityCommand) -> SourceIdentityCorrectionReport:
        pass


class _Planner(Protocol):
    def plan(self, *, review_id: str, company_id: int) -> ProjectionSyncResult:
        pass


#: ``(apply) -> context manager yielding a ready use case`` -- one per review.
UseCaseScope = Callable[[bool], AbstractContextManager[_UseCase]]


@dataclass(frozen=True, slots=True)
class ReviewTarget:
    review_id: str
    expected_version: int


def parse_review_target(value: str) -> ReviewTarget:
    review_id, separator, version = value.rpartition("@")
    if not separator or not review_id.strip() or not version.isdigit() or int(version) <= 0:
        raise argparse.ArgumentTypeError("expected REVIEW_ID@EXPECTED_VERSION, e.g. review:<uuid>@1")
    return ReviewTarget(review_id=review_id.strip(), expected_version=int(version))


def run_corrections(
    scope: UseCaseScope,
    *,
    targets: Sequence[ReviewTarget],
    company_id: int,
    approved_by: str,
    apply: bool,
    out: TextIO,
    planner: _Planner | None = None,
    field_path: SourceInvoiceCorrectionField = SourceInvoiceCorrectionField.SUPPLIER_TAX_NUMBER,
    reason: SourceInvoiceCorrectionReason = SourceInvoiceCorrectionReason.UBL_PARTY_TAX_IDENTIFIER_PR201,
) -> Counter[str]:
    mode = "APPLY" if apply else "DRY-RUN (no Hub or Odoo writes)"
    out.write(
        f"Review source identity correction | company {company_id} | {field_path.value} | {reason.value} | {mode}\n"
    )
    totals: Counter[str] = Counter()
    for target in targets:
        out.write(f"\n=== {target.review_id} (expected v{target.expected_version})\n")
        try:
            command = CorrectReviewSourceIdentityCommand(
                review_id=target.review_id,
                company_id=company_id,
                expected_version=target.expected_version,
                approved_by=approved_by,
                apply=apply,
                field_path=field_path,
                reason=reason,
            )
            with scope(apply) as use_case:
                report = asyncio.run(use_case.execute(command))
        except Exception as exc:  # noqa: BLE001 - one review never stops the run; reported, not swallowed
            totals["ERROR"] += 1
            out.write(f"ERROR         {type(exc).__name__}: {getattr(exc, 'safe_message', None) or exc}\n")
            continue
        totals[report.outcome.value] += 1
        _write_report(out, report)
        if planner is not None and not apply:
            _write_current_projection(out, planner, review_id=target.review_id, company_id=company_id)
    summary = " ".join(f"{label}={totals.get(label, 0)}" for label in _outcome_labels())
    out.write(f"\nSummary: {summary} | reviews={len(targets)} | applied={apply}\n")
    return totals


def _outcome_labels() -> tuple[str, ...]:
    return (*(outcome.value for outcome in SourceIdentityCorrectionOutcome), "ERROR")


def _write_report(out: TextIO, report: SourceIdentityCorrectionReport) -> None:
    out.write(f"{report.outcome.value:<14}{report.safe_message or ''}\n")
    if report.from_version is not None:
        to_version = f"v{report.to_version}" if report.to_version is not None else "(unchanged)"
        out.write(
            f"  {report.field_path.value}: {report.old_value!r} -> {report.new_value!r}"
            f" | version: v{report.from_version} -> {to_version}\n"
        )
    if report.new_workflow is not None:
        out.write(
            f"  workflow: {report.previous_workflow} -> {report.new_workflow}"
            f" | classification: {report.classification_status}\n"
            f"  reasons: {list(report.previous_reason_codes)} -> {list(report.new_reason_codes)}\n"
        )
    out.write("  preconditions:\n")
    for check in report.checks:
        out.write(f"    [{check.status.value}] {check.name}: {check.detail}\n")
    if report.hub_changes:
        out.write("  Hub changes (one transaction):\n")
        for change in report.hub_changes:
            if change.field == "INSERT":
                out.write(f"    INSERT {change.target}: {change.after}\n")
            else:
                out.write(
                    f"    UPDATE {change.target}.{change.field}: {_value(change.before)} -> {_value(change.after)}\n"
                )
    if report.projection_changes:
        out.write("  Odoo Workbench projection changes (after commit):\n")
        for change in report.projection_changes:
            out.write(f"    {change.field}: {_value(change.before)} -> {_value(change.after)}\n")
    if report.applied:
        record = f" odoo_id={report.projection_odoo_record_id}" if report.projection_odoo_record_id else ""
        error = f" error={report.projection_error}" if report.projection_error else ""
        out.write(f"  projection: {report.projection_outcome}{record}{error}\n")


def _write_current_projection(out: TextIO, planner: _Planner, *, review_id: str, company_id: int) -> None:
    try:
        result = planner.plan(review_id=review_id, company_id=company_id)
    except Exception as exc:  # noqa: BLE001 - informational, read-only
        out.write(f"  current Workbench row: unavailable ({type(exc).__name__})\n")
        return
    out.write(
        f"  current Workbench row: odoo_id={result.odoo_record_id} v{result.review_version} "
        f"reconcile={result.outcome.value}\n"
    )


def _value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, str) and value.startswith("<"):
        return value
    return repr(value)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.correct_review_source_identity",
        description="Audited append-only correction of review source-invoice identity (dry-run by default).",
    )
    parser.add_argument("--company", type=int, required=True, help="Hub/Odoo company id (e.g. 1).")
    parser.add_argument(
        "--review",
        dest="reviews",
        action="append",
        required=True,
        type=parse_review_target,
        help="REVIEW_ID@EXPECTED_VERSION to correct (repeatable). Only these reviews are processed.",
    )
    parser.add_argument("--approved-by", required=True, help="Operator identity recorded on each correction.")
    parser.add_argument(
        "--field",
        choices=[item.value for item in SourceInvoiceCorrectionField],
        default=SourceInvoiceCorrectionField.SUPPLIER_TAX_NUMBER.value,
    )
    parser.add_argument(
        "--reason",
        choices=[item.value for item in SourceInvoiceCorrectionReason],
        default=SourceInvoiceCorrectionReason.UBL_PARTY_TAX_IDENTIFIER_PR201.value,
    )
    parser.add_argument("--apply", action="store_true", help="Commit the corrections. Default is a zero-write dry-run.")
    args = parser.parse_args(argv)
    if args.company <= 0:
        parser.error("--company must be a positive id.")
    if not args.approved_by.strip():
        parser.error("--approved-by must be non-empty.")
    seen = [target.review_id for target in args.reviews]
    if len(seen) != len(set(seen)):
        parser.error("each review may be listed only once.")
    return args


def main(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO | None = None,
    engine: object | None = None,
    scope: UseCaseScope | None = None,
    planner: _Planner | None = None,
) -> int:
    args = _parse_args(argv)
    stream = out or sys.stdout
    if scope is None:
        try:
            scope, planner = _production_scope(engine)
        except Exception as exc:  # noqa: BLE001 - nothing was read or written yet
            print(f"Configuration error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_CONFIGURATION_ERROR
    totals = run_corrections(
        scope,
        targets=args.reviews,
        company_id=args.company,
        approved_by=args.approved_by.strip(),
        apply=args.apply,
        out=stream,
        planner=planner,
        field_path=SourceInvoiceCorrectionField(args.field),
        reason=SourceInvoiceCorrectionReason(args.reason),
    )
    failed = any(label not in _SUCCESS_OUTCOMES for label, count in totals.items() if count)
    return EXIT_REVIEW_FAILURES if failed else 0


def _production_scope(engine: object | None) -> tuple[UseCaseScope, _Planner | None]:  # pragma: no cover - wiring
    from contextlib import contextmanager

    from sqlalchemy.orm import Session

    from app.composition.imports import (
        build_runtime_workbench_projection_synchronizer,
        build_workbench_projection_synchronizer,
        open_read_only_session,
    )
    from app.composition.source_identity_correction import build_correct_review_source_identity_use_case
    from app.connectors.odoo.client import OdooJson2Client
    from app.core.config import get_settings

    if engine is None:
        from app.db.session import engine as default_engine

        engine = default_engine
    settings = get_settings()
    odoo_client = OdooJson2Client.from_settings(settings)
    planner = (
        build_workbench_projection_synchronizer(engine=engine, settings=settings, odoo_client=odoo_client)
        if settings.odoo_workbench_projection_publish_enabled
        else None
    )

    @contextmanager
    def scope(apply: bool):
        if not apply:
            with open_read_only_session(engine) as read_session:
                yield build_correct_review_source_identity_use_case(
                    session=read_session, settings=settings, odoo_client=odoo_client
                )
            return
        with Session(bind=engine, autoflush=False) as session:
            yield build_correct_review_source_identity_use_case(
                session=session,
                settings=settings,
                odoo_client=odoo_client,
                projection_synchronizer=build_runtime_workbench_projection_synchronizer(
                    session=session, settings=settings, odoo_client=odoo_client
                ),
            )

    return scope, planner


if __name__ == "__main__":  # pragma: no cover - thin process entry point
    raise SystemExit(main())
