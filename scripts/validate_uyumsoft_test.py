from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.connectors.uyumsoft.client import UyumsoftSoapClient  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.document_storage import LocalDocumentStorage  # noqa: E402
from app.services.uyumsoft_test_validation import (  # noqa: E402
    UyumsoftTestValidationService,
    _json_dumps,
    default_validation_request,
    write_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate read-only Uyumsoft test connectivity and UBL acquisition.")
    parser.add_argument("--limit", type=int, default=5, help="Maximum incoming invoices to inspect, 1-100.")
    parser.add_argument("--from-date", help="Inclusive ISO datetime. Defaults to seven days before --to-date.")
    parser.add_argument("--to-date", help="Inclusive ISO datetime. Defaults to now.")
    parser.add_argument("--output-report", type=Path, help="Optional path for the sanitized JSON report.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print the sanitized JSON report.")
    args = parser.parse_args()

    settings = get_settings()
    request = default_validation_request(
        limit=args.limit,
        from_date=_parse_datetime(args.from_date) if args.from_date else None,
        to_date=_parse_datetime(args.to_date) if args.to_date else None,
    )
    with SessionLocal() as session:
        report = UyumsoftTestValidationService(
            settings=settings,
            client=UyumsoftSoapClient.from_settings(settings),
            session=session,
            storage=LocalDocumentStorage(settings.document_storage_root),
        ).validate(request)
        session.commit()

    output = _json_dumps(report, pretty=args.pretty)
    print(output)
    if args.output_report is not None:
        write_report(args.output_report, report)
    return 0 if report["overall_status"] == "ok" else 1


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
