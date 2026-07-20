import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.schemas.uyumsoft_invoices import (
    UyumsoftInvoiceListRequest,
    UyumsoftInvoiceListResponse,
    UyumsoftInvoiceSummary,
)
from scripts import uyumsoft_readonly_smoke


def test_smoke_script_refuses_without_live_enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(uyumsoft_readonly_smoke.ENABLE_FLAG, raising=False)
    monkeypatch.setattr(sys, "argv", ["uyumsoft_readonly_smoke.py"])

    with pytest.raises(SystemExit) as exc_info:
        uyumsoft_readonly_smoke.main()

    assert str(exc_info.value) == "Live smoke check is disabled. Set ICT_UYUMSOFT_ENABLE_LIVE_SMOKE=1 to run it."


def test_smoke_script_refuses_page_size_other_than_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(uyumsoft_readonly_smoke.ENABLE_FLAG, "1")
    monkeypatch.setattr(sys, "argv", ["uyumsoft_readonly_smoke.py", "--page-size", "2"])

    with pytest.raises(SystemExit) as exc_info:
        uyumsoft_readonly_smoke.main()

    assert str(exc_info.value) == "Live smoke check requires --page-size 1."


def test_smoke_script_refuses_production_connector_without_live_readonly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(uyumsoft_readonly_smoke.ENABLE_FLAG, "1")
    monkeypatch.setattr(sys, "argv", ["uyumsoft_readonly_smoke.py"])
    monkeypatch.setattr(
        uyumsoft_readonly_smoke,
        "get_settings",
        lambda: Settings(
            app_env="development",
            uyumsoft_environment="production",
            live_connector_readonly=False,
            uyumsoft_username="live-user",
            uyumsoft_password=SecretStr("live-password"),
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        uyumsoft_readonly_smoke.main()

    assert str(exc_info.value) == "Live production connector smoke requires LIVE_CONNECTOR_READONLY=true."


def test_smoke_run_only_reaches_read_only_list_operations() -> None:
    client = RecordingSmokeClient()
    request = UyumsoftInvoiceListRequest(
        from_date=datetime(2026, 7, 19, tzinfo=UTC),
        to_date=datetime(2026, 7, 20, tzinfo=UTC),
        page=1,
        page_size=1,
    )

    result = uyumsoft_readonly_smoke._run_smoke(client, request)

    assert result["inbox"]["ok"] is True
    assert result["outbox"]["ok"] is True
    assert client.calls == ["GetInboxInvoiceList", "GetOutboxInvoiceList"]


class RecordingSmokeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_inbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        self.calls.append("GetInboxInvoiceList")
        return _response("Inbox", request)

    def list_outbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        self.calls.append("GetOutboxInvoiceList")
        return _response("Outbox", request)

    def __getattribute__(self, name: str) -> Any:
        forbidden = {"SetInvoicesTaken", "SendInvoice", "CancelInvoice", "RetrySendInvoices", "MoveToDraftStatus"}
        if name in forbidden:
            raise AssertionError(f"Forbidden operation accessed: {name}")
        return super().__getattribute__(name)


def _response(direction: str, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
    return UyumsoftInvoiceListResponse(
        direction=direction,
        page=request.page,
        page_size=request.page_size,
        total_count=1,
        invoices=[
            UyumsoftInvoiceSummary(
                invoice_id=f"{direction.lower()}-1",
                ettn=f"{direction.lower()}-ettn",
                invoice_number=f"{direction}-INV-1",
                invoice_date=datetime(2026, 7, 20, tzinfo=UTC),
                sender="Sender",
                receiver="Receiver",
                tax_number="1234567890",
                currency="TRY",
                total_amount=Decimal("10.00"),
                direction=direction,
                status="NEW",
            )
        ],
    )
