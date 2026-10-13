"""PR F / F2: advisory product suggestions -- backfill and history cleanup (ADR-0014).

    python -m app.cli.advisory_product_suggestions generate --company 1                 # dry-run
    python -m app.cli.advisory_product_suggestions generate --company 1 --apply --confirm-apply
    python -m app.cli.advisory_product_suggestions cleanup --company 1                  # dry-run
    python -m app.cli.advisory_product_suggestions cleanup --company 1 --apply --confirm-apply

Dry-run is the default and writes nothing: the Hub is read on a read-only session
(PostgreSQL ``READ ONLY`` transaction) and Odoo only through the bounded read-only catalog
reader. It reports every current PRODUCT_NOT_FOUND line with every candidate, tier, score,
reason, warning and ambiguity, and every set that would be superseded or invalidated.

``--apply`` writes advisory rows in the Hub only (never Odoo) and requires the separate
``--confirm-apply`` flag as explicit operator intent. ``generate --apply`` runs exactly one
single-flight worker tick; ``cleanup --apply`` deletes superseded/invalidated sets older
than the retention (90 days by default). Nothing here changes matching, mapping, creation,
supplierinfo, Internal References, decisions or accounting.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TextIO

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.advisory.suggestion_sets import HISTORY_RETENTION
from app.composition.advisory import advisory_lock, advisory_policy, advisory_service, odoo_catalog_reader
from app.composition.imports import open_read_only_session
from app.core.config import Settings, get_settings
from app.persistence.product_line_suggestion_repository import (
    SqlAlchemyProductLineSuggestionRepository,
    history_cutoff,
)
from app.services.advisory_product_suggestions import AdvisoryCatalogReader, AdvisoryPlan, AdvisoryTickStatus

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
_DESCRIPTION_PREVIEW = 60


def main(
    argv: Sequence[str] | None = None,
    *,
    engine: Engine | None = None,
    settings: Settings | None = None,
    catalog_reader: AdvisoryCatalogReader | None = None,
    now: datetime | None = None,
    out: TextIO | None = None,
) -> int:
    args = _parse_args(argv)
    stream = out or sys.stdout
    if args.apply and not args.confirm_apply:
        print("--apply requires --confirm-apply (explicit operator intent). Nothing was written.", file=stream)
        return EXIT_USAGE
    resolved_settings = settings or get_settings()
    bound_engine = engine or _engine()
    if args.command == "generate":
        reader = catalog_reader or odoo_catalog_reader(resolved_settings)
        if args.apply:
            return _generate_apply(bound_engine, resolved_settings, reader, args.company, stream)
        return _generate_dry_run(bound_engine, resolved_settings, reader, args.company, stream)
    return _cleanup(bound_engine, args, now or datetime.now(UTC), stream)


def _generate_dry_run(
    engine: Engine, settings: Settings, reader: AdvisoryCatalogReader, company_id: int, out: TextIO
) -> int:
    with open_read_only_session(engine) as session:
        plan = advisory_service(session, catalog_reader=reader, policy=advisory_policy(settings)).plan(
            company_id=company_id
        )
    write_plan(plan, company_id=company_id, out=out)
    return EXIT_FAILED if plan.status is AdvisoryTickStatus.CATALOG_FAILED else EXIT_OK


def _generate_apply(
    engine: Engine, settings: Settings, reader: AdvisoryCatalogReader, company_id: int, out: TextIO
) -> int:
    with advisory_lock(engine).hold() as acquired:
        if not acquired:
            print("Another advisory tick holds the lock; nothing was written.", file=out)
            return EXIT_FAILED
        session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
        try:
            result = advisory_service(session, catalog_reader=reader, policy=advisory_policy(settings)).run(
                company_id=company_id
            )
        finally:
            session.close()
    print(
        f"Advisory suggestions | company {company_id} | APPLY (Hub advisory rows only) | status={result.status} "
        f"lines={result.lines_seen} generated={result.generated} revalidated={result.revalidated} "
        f"retired={result.retired} failed_lines={result.failed_lines} truncated={result.truncated}",
        file=out,
    )
    return EXIT_FAILED if result.status is AdvisoryTickStatus.CATALOG_FAILED or result.failed_lines else EXIT_OK


def _cleanup(engine: Engine, args: argparse.Namespace, now: datetime, out: TextIO) -> int:
    cutoff = history_cutoff(now, timedelta(days=args.older_than_days))
    if not args.apply:
        with open_read_only_session(engine) as session:
            count = SqlAlchemyProductLineSuggestionRepository(session).count_history_older_than(
                company_id=args.company, cutoff=cutoff
            )
        print(
            f"Advisory suggestion history cleanup | company {args.company} | DRY-RUN | "
            f"superseded/invalidated sets older than {cutoff.isoformat()}: {count}",
            file=out,
        )
        return EXIT_OK
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        deleted = SqlAlchemyProductLineSuggestionRepository(session).delete_history_older_than(
            company_id=args.company, cutoff=cutoff
        )
        session.commit()
    finally:
        session.close()
    print(f"Advisory suggestion history cleanup | company {args.company} | APPLY | deleted sets: {deleted}", file=out)
    return EXIT_OK


def write_plan(plan: AdvisoryPlan, *, company_id: int, out: TextIO) -> None:
    out.write(f"Advisory product suggestions | company {company_id} | DRY-RUN (no Hub or Odoo writes)\n")
    out.write(f"status={plan.status} lines={plan.lines_seen} truncated={plan.truncated}\n")
    if plan.catalog_error:
        out.write(f"catalog read failed: {plan.catalog_error} (nothing would be regenerated or revalidated)\n")
    for retirement in plan.retirements:
        stored = retirement.stored
        out.write(
            f"RETIRE {retirement.status}/{retirement.reason} {stored.review_id} v{stored.review_version} "
            f"line {stored.line_number}\n"
        )
    tiers: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    for planned in plan.lines:
        line, result = planned.line, planned.result
        actions[planned.action.value] += 1
        reason = f" ({planned.reason})" if planned.reason else ""
        description = (line.description or "")[:_DESCRIPTION_PREVIEW]
        out.write(
            f"{planned.action.value.upper():<10}{reason} {line.review_id} v{line.review_version} "
            f"line {line.line_number} supplier #{line.supplier_partner_id} code={line.seller_item_code!r} "
            f"| {description}\n"
        )
        if result is None:
            continue
        flags = [
            flag
            for flag, on in (("AMBIGUOUS", result.ambiguous), ("RECLASSIFY", result.reclassification_recommended))
            if on
        ]  # noqa: E501
        if flags:
            out.write(f"    flags: {', '.join(flags)}\n")
        if not result.suggestions:
            out.write("    no suggestion\n")
        for suggestion in result.suggestions:
            tiers[suggestion.tier.value] += 1
            out.write(
                f"    #{suggestion.rank} {suggestion.tier.value} score={suggestion.score} "
                f"product={suggestion.product_id} | {'; '.join(suggestion.reasons)}"
                + (f" | WARN: {'; '.join(suggestion.warnings)}" if suggestion.warnings else "")
                + "\n"
            )
        for note in result.notes:
            out.write(f"    note: {note}\n")
    out.write(
        "Summary: "
        + " ".join(f"{name}={count}" for name, count in sorted(actions.items()))
        + " | candidates by tier: "
        + (" ".join(f"{name}={count}" for name, count in sorted(tiers.items())) or "none")
        + f" | retire={len(plan.retirements)}\n"
    )


def _engine() -> Engine:
    from app.db.session import engine

    return engine


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.cli.advisory_product_suggestions")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("generate", "Plan (default) or apply one advisory suggestion tick."),
        ("cleanup", "Report (default) or delete superseded/invalidated history beyond the retention."),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--company", type=int, required=True)
        command.add_argument("--apply", action="store_true", help="Write Hub advisory rows (never Odoo).")
        command.add_argument("--confirm-apply", action="store_true", help="Required together with --apply.")
        if name == "cleanup":
            command.add_argument("--older-than-days", type=int, default=HISTORY_RETENTION.days)
    return parser.parse_args(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
