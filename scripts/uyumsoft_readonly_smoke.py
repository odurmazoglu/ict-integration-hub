from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.exceptions import ConnectorError
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.core.config import get_settings
from app.schemas.uyumsoft_invoices import UyumsoftInvoiceListRequest, UyumsoftInvoiceListResponse

ENABLE_FLAG = "ICT_UYUMSOFT_ENABLE_LIVE_SMOKE"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an opt-in read-only Uyumsoft invoice-list smoke test.")
    parser.add_argument("--from", dest="from_date", required=False, help="Inclusive ISO datetime.")
    parser.add_argument("--to", dest="to_date", required=False, help="Inclusive ISO datetime.")
    parser.add_argument("--page-size", type=int, default=1)
    args = parser.parse_args()

    if os.getenv(ENABLE_FLAG) != "1":
        raise SystemExit(f"Live smoke check is disabled. Set {ENABLE_FLAG}=1 to run it.")
    if args.page_size != 1:
        raise SystemExit("Live smoke check requires --page-size 1.")

    settings = get_settings()
    _validate_live_readonly_mode(settings)

    to_date = _parse_datetime(args.to_date) if args.to_date else datetime.now(tz=UTC)
    from_date = _parse_datetime(args.from_date) if args.from_date else to_date - timedelta(days=1)
    if from_date > to_date:
        raise SystemExit("--from must be before or equal to --to.")

    client = UyumsoftSoapClient.from_settings(settings)
    request = UyumsoftInvoiceListRequest(from_date=from_date, to_date=to_date, page=1, page_size=args.page_size)

    print(json.dumps(_run_smoke(client, request), indent=2, default=str))


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _safe_summary(response: UyumsoftInvoiceListResponse) -> dict[str, Any]:
    return {
        "direction": response.direction,
        "page": response.page,
        "page_size": response.page_size,
        "total_count": response.total_count,
        "returned_count": len(response.invoices),
        "first_invoice": _safe_invoice_summary(response) if response.invoices else None,
    }


def _run_smoke(client: UyumsoftSoapClient, request: UyumsoftInvoiceListRequest) -> dict[str, Any]:
    return {
        "inbox": _call_safely(lambda: client.list_inbox_invoices(request)),
        "outbox": _call_safely(lambda: client.list_outbox_invoices(request)),
    }


def _validate_live_readonly_mode(settings: Any) -> None:
    if settings.app_env != "production" and settings.uyumsoft_environment == "production":
        if not settings.live_connector_readonly:
            raise SystemExit("Live production connector smoke requires LIVE_CONNECTOR_READONLY=true.")


def _call_safely(callback: Any) -> dict[str, Any]:
    try:
        return {"ok": True, "result": _safe_summary(callback())}
    except ConnectorError as exc:
        return {"ok": False, "error": exc.safe_message}


def _safe_invoice_summary(response: UyumsoftInvoiceListResponse) -> dict[str, Any]:
    invoice = response.invoices[0]
    return {
        "has_invoice_id": invoice.invoice_id is not None,
        "has_ettn": invoice.ettn is not None,
        "has_invoice_number": invoice.invoice_number is not None,
        "invoice_date": invoice.invoice_date,
        "currency": invoice.currency,
        "total_amount": invoice.total_amount,
        "direction": response.direction,
        "status": invoice.status,
    }


if __name__ == "__main__":
    main()
