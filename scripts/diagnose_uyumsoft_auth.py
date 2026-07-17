from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from lxml import etree
from requests import Response
from zeep import Client
from zeep import Settings as ZeepSettings
from zeep.exceptions import Fault, TransportError
from zeep.plugins import HistoryPlugin
from zeep.transports import Transport
from zeep.wsse.username import UsernameToken

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.uyumsoft.client import _sanitize_fault_message
from app.core.config import get_settings

ENABLE_FLAG = "ICT_UYUMSOFT_ENABLE_LIVE_SMOKE"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
WSSE_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
NS = {"soapenv": SOAP_NS, "wsse": WSSE_NS, "wsu": WSU_NS}


class DiagnosticTransport(Transport):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.last_status_code: int | None = None
        self.last_response_headers: dict[str, str] = {}

    def post_xml(self, address: str, envelope: etree._Element, headers: dict[str, str]) -> Response:
        response = super().post_xml(address, envelope, headers)
        self.last_status_code = response.status_code
        self.last_response_headers = dict(response.headers)
        return response


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a safe Uyumsoft SOAP authentication diagnostic.")
    parser.add_argument("--from", dest="from_date", required=False, help="Inclusive ISO datetime.")
    parser.add_argument("--to", dest="to_date", required=False, help="Inclusive ISO datetime.")
    args = parser.parse_args()

    if os.getenv(ENABLE_FLAG) != "1":
        raise SystemExit(f"Live diagnostic is disabled. Set {ENABLE_FLAG}=1 to run it.")

    to_date = _parse_datetime(args.to_date) if args.to_date else datetime.now(tz=UTC)
    from_date = _parse_datetime(args.from_date) if args.from_date else to_date - timedelta(days=1)
    if from_date > to_date:
        raise SystemExit("--from must be before or equal to --to.")

    settings = get_settings()
    transport = DiagnosticTransport(
        timeout=settings.uyumsoft_timeout_seconds,
        operation_timeout=settings.uyumsoft_timeout_seconds,
    )
    history = HistoryPlugin()
    client = Client(
        wsdl=settings.uyumsoft_wsdl_url,
        transport=transport,
        plugins=[history],
        settings=ZeepSettings(strict=False),
        wsse=UsernameToken(
            settings.uyumsoft_username,
            settings.uyumsoft_password.get_secret_value(),
            use_digest=False,
        ),
    )
    query_model = client.get_type("{http://tempuri.org/}InboxInvoiceListQueryModel")
    query = query_model(
        ExecutionStartDate=from_date,
        ExecutionEndDate=to_date,
        PageIndex=1,
        PageSize=1,
        IncludeTagList=False,
        OnlyNewestInvoices=False,
    )

    reports = [
        _call_safely(
            "TestConnection",
            lambda: client.service.TestConnection(),
            transport,
            history,
            settings.uyumsoft_username,
        ),
        _call_safely(
            "WhoAmI",
            lambda: client.service.WhoAmI(),
            transport,
            history,
            settings.uyumsoft_username,
        ),
        _call_safely(
            "GetInboxInvoiceList",
            lambda: client.service.GetInboxInvoiceList(query),
            transport,
            history,
            settings.uyumsoft_username,
        ),
    ]
    print(json.dumps(reports, indent=2, default=str))


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _call_safely(
    operation: str,
    callback: Any,
    transport: DiagnosticTransport,
    history: HistoryPlugin,
    username: str,
) -> dict[str, Any]:
    try:
        result = callback()
        return _report(operation, transport, history, username, ok=True, result_type=type(result).__name__)
    except Fault as exc:
        return _report(
            operation,
            transport,
            history,
            username,
            ok=False,
            error_type="Fault",
            fault_code=str(getattr(exc, "code", "") or ""),
            fault_reason=_sanitize_fault_message(str(exc)),
        )
    except TransportError as exc:
        return _report(
            operation,
            transport,
            history,
            username,
            ok=False,
            error_type="TransportError",
            http_status=exc.status_code,
        )
    except Exception as exc:
        return _report(operation, transport, history, username, ok=False, error_type=type(exc).__name__, error=str(exc))


def _report(
    operation: str,
    transport: DiagnosticTransport,
    history: HistoryPlugin,
    username: str,
    **fields: Any,
) -> dict[str, Any]:
    sent = _last_envelope(history, sent=True)
    received = _last_envelope(history, sent=False)
    return {
        "operation": operation,
        "endpoint_url": _last_header(history, "To"),
        "soap_action": _last_header(history, "Action"),
        "http_status": fields.pop("http_status", transport.last_status_code),
        "security_mechanism_detected": "WS-Security UsernameToken PasswordText",
        "request": _request_characteristics(sent),
        "response": _response_characteristics(received),
        "classification": _classify(fields, username),
        **fields,
    }


def _last_envelope(history: HistoryPlugin, *, sent: bool) -> etree._Element | None:
    try:
        item = history.last_sent if sent else history.last_received
        return item.get("envelope") if item else None
    except Exception:
        return None


def _last_header(history: HistoryPlugin, name: str) -> str | None:
    sent = _last_envelope(history, sent=True)
    if sent is None:
        return None
    values = sent.xpath(
        f"/soapenv:Envelope/soapenv:Header/*[local-name()='{name}']/text()",
        namespaces=NS,
    )
    return str(values[0]) if values else None


def _request_characteristics(envelope: etree._Element | None) -> dict[str, Any]:
    if envelope is None:
        return {}
    password_types = envelope.xpath("//wsse:Password/@Type", namespaces=NS)
    return {
        "soap_namespaces": sorted(
            {etree.QName(element).namespace for element in envelope.iter() if etree.QName(element).namespace}
        ),
        "has_ws_security": bool(envelope.xpath("boolean(//wsse:Security)", namespaces=NS)),
        "has_username_token": bool(envelope.xpath("boolean(//wsse:UsernameToken)", namespaces=NS)),
        "password_type": password_types[0].rsplit("#", 1)[-1] if password_types else None,
        "has_nonce": bool(envelope.xpath("boolean(//wsse:Nonce)", namespaces=NS)),
        "has_created": bool(envelope.xpath("boolean(//wsu:Created)", namespaces=NS)),
        "has_timestamp": bool(envelope.xpath("boolean(//wsu:Timestamp)", namespaces=NS)),
        "body_operation": _body_operation(envelope),
    }


def _response_characteristics(envelope: etree._Element | None) -> dict[str, Any]:
    if envelope is None:
        return {}
    fault = envelope.xpath("//soapenv:Fault", namespaces=NS)
    response: dict[str, Any] = {
        "body_operation": _body_operation(envelope),
        "has_fault": bool(fault),
    }
    if fault:
        node = fault[0]
        response["soap_fault_code"] = "".join(node.xpath("./faultcode/text()")) or None
        fault_reason = "".join(node.xpath("./faultstring/text()")) or None
        response["soap_fault_reason"] = _sanitize_fault_message(fault_reason) if fault_reason else None
        detail = "".join(node.xpath("./detail//text()")).strip()
        response["soap_fault_detail_present"] = bool(detail)
        if detail:
            response["safe_detail_length"] = len(detail)
    return response


def _body_operation(envelope: etree._Element) -> str | None:
    body_children = envelope.xpath("/soapenv:Envelope/soapenv:Body/*", namespaces=NS)
    return etree.QName(body_children[0]).localname if body_children else None


def _classify(fields: dict[str, Any], username: str) -> str:
    fault_reason = str(fields.get("fault_reason", ""))
    fault_code = str(fields.get("fault_code", ""))
    if username == "change-me":
        return "Missing credentials"
    if "gerekli yetkiniz yok" in fault_reason:
        return "Provider authorization"
    if "InvalidSecurity" in fault_code or "verifying security" in fault_reason:
        return "Client implementation"
    if fields.get("error_type") == "TransportError":
        return "Network"
    if fields.get("ok") is True:
        return "Authenticated"
    return "Unknown"


if __name__ == "__main__":
    main()
