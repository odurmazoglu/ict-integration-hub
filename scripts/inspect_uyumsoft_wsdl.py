from __future__ import annotations

import argparse
import json
from typing import Any

from zeep import Client
from zeep.transports import Transport

DEFAULT_WSDL_URL = "https://efatura-test.uyumsoft.com.tr/Services/Integration?wsdl"
TARGET_OPERATIONS = ("GetInboxInvoiceList", "GetOutboxInvoiceList")
TARGET_TYPES = (
    "InboxInvoiceListQueryModel",
    "OutboxInvoiceListQueryModel",
    "InboxInvoiceListResponse",
    "OutboxInvoiceListResponse",
    "PagedResponseOfInboxInvoiceListItem",
    "PagedResponseOfOutboxInvoiceListItem",
    "InboxInvoiceListItem",
    "OutboxInvoiceListItem",
)
UYUMSOFT_NAMESPACE = "http://tempuri.org/"


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect safe Uyumsoft invoice-list WSDL metadata.")
    parser.add_argument("--wsdl-url", default=DEFAULT_WSDL_URL)
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()

    client = Client(
        wsdl=args.wsdl_url,
        transport=Transport(timeout=args.timeout, operation_timeout=args.timeout),
    )
    print(json.dumps(discover_invoice_list_schema(client), indent=2, ensure_ascii=False, default=str))


def discover_invoice_list_schema(client: Client) -> dict[str, Any]:
    return {
        "operations": _discover_operations(client),
        "types": {type_name: _describe_type(client, type_name) for type_name in TARGET_TYPES},
    }


def _discover_operations(client: Client) -> dict[str, Any]:
    operations: dict[str, Any] = {}
    for service in client.wsdl.services.values():
        for port in service.ports.values():
            for operation_name, operation in sorted(port.binding._operations.items()):
                if operation_name not in TARGET_OPERATIONS:
                    continue
                operations[operation_name] = {
                    "service": service.name,
                    "port": port.name,
                    "input": operation.input.signature(),
                    "output": operation.output.signature(),
                }
    return operations


def _describe_type(client: Client, type_name: str) -> dict[str, Any]:
    zeep_type = client.get_type(f"{{{UYUMSOFT_NAMESPACE}}}{type_name}")
    signature = zeep_type.signature()
    return {
        "signature": signature,
        "signature_fields": _parse_signature_fields(signature),
        "fields": [_describe_element(name, element) for name, element in zeep_type.elements],
    }


def _describe_element(name: str, element: Any) -> dict[str, Any]:
    return {
        "name": name,
        "type": str(element.type),
        "min_occurs": getattr(element, "min_occurs", None),
        "max_occurs": str(getattr(element, "max_occurs", None)),
        "nillable": getattr(element, "nillable", None),
    }


def _parse_signature_fields(signature: str) -> list[dict[str, str]]:
    start = signature.find("(")
    end = signature.rfind(")")
    if start == -1 or end == -1 or end <= start:
        return []

    return [
        {"name": name.strip(), "type": field_type.strip()}
        for field in signature[start + 1 : end].split(", ")
        if ": " in field
        for name, field_type in [field.split(": ", 1)]
    ]


if __name__ == "__main__":
    main()
