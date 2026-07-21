from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from zeep import Client
from zeep.transports import Transport

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_WSDL_URL = "https://efatura-test.uyumsoft.com.tr/Services/Integration?wsdl"
UYUMSOFT_NAMESPACE = "http://tempuri.org/"
DEFAULT_QUERY_MODELS = ("InboxInvoiceListQueryModel", "OutboxInvoiceListQueryModel")


@dataclass(frozen=True)
class FieldMetadata:
    name: str
    type: str
    discovery_path: str


@dataclass(frozen=True)
class ModelMetadata:
    model: str
    factory_type: str
    underlying_zeep_type: str
    fields: list[FieldMetadata]
    discovery_path: list[str]


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    _reject_url_credentials(args.wsdl_url)
    client = Client(
        wsdl=args.wsdl_url,
        transport=Transport(timeout=args.timeout, operation_timeout=args.timeout),
    )

    if args.list_models:
        models = list_models(client)
        if args.json:
            print(json.dumps({"models": models}, indent=2, ensure_ascii=False))
        else:
            _print_models(models)
        return 0

    metadata = inspect_model(client, args.model)
    if args.json:
        print(json.dumps(asdict(metadata), indent=2, ensure_ascii=False))
    else:
        _print_model(metadata)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect safe Uyumsoft Zeep WSDL model metadata. This utility only loads "
            "the WSDL and never invokes SOAP business operations."
        )
    )
    parser.add_argument("--wsdl-url", default=DEFAULT_WSDL_URL, help="Uyumsoft WSDL URL to load.")
    parser.add_argument("--timeout", type=float, default=20, help="WSDL loading timeout in seconds.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list-models", action="store_true", help="List available complex/query models.")
    action.add_argument("--model", help="Inspect one WSDL model, for example InboxInvoiceListQueryModel.")
    return parser


def list_models(client: Client) -> list[str]:
    discovered = set(DEFAULT_QUERY_MODELS)
    for zeep_type in _iter_wsdl_types(client):
        name = _local_name(getattr(zeep_type, "qname", None)) or _local_name(getattr(zeep_type, "name", None))
        if not name:
            continue
        if _has_model_fields(zeep_type) and ("Model" in name or "Query" in name):
            discovered.add(name)
    return sorted(discovered)


def inspect_model(client: Client, model_name: str) -> ModelMetadata:
    factory = client.type_factory(UYUMSOFT_NAMESPACE)
    zeep_type = client.get_type(f"{{{UYUMSOFT_NAMESPACE}}}{model_name}")
    fields, paths = _discover_fields(zeep_type)
    return ModelMetadata(
        model=model_name,
        factory_type=type(factory).__name__,
        underlying_zeep_type=str(zeep_type),
        fields=fields,
        discovery_path=paths,
    )


def _iter_wsdl_types(client: Client) -> Iterable[Any]:
    types_container = getattr(getattr(client, "wsdl", None), "types", None)
    if types_container is None:
        return []
    types = getattr(types_container, "types", [])
    return types() if callable(types) else types


def _discover_fields(zeep_type: Any) -> tuple[list[FieldMetadata], list[str]]:
    fields: dict[str, FieldMetadata] = {}
    paths: list[str] = []
    _collect_fields(zeep_type, "type", fields, paths, set())
    return list(fields.values()), paths


def _collect_fields(
    value: Any,
    path: str,
    fields: dict[str, FieldMetadata],
    paths: list[str],
    visited: set[int],
) -> None:
    if value is None:
        return
    marker = id(value)
    if marker in visited:
        return
    visited.add(marker)

    for attr in ("elements", "_xsd_type", "sequence"):
        nested = getattr(value, attr, None)
        if nested is None:
            continue
        nested_path = f"{path}.{attr}"
        if attr == "elements":
            _collect_element_entries(nested, nested_path, fields, paths, visited)
        else:
            _collect_fields(nested, nested_path, fields, paths, visited)


def _collect_element_entries(
    entries: Any,
    path: str,
    fields: dict[str, FieldMetadata],
    paths: list[str],
    visited: set[int],
) -> None:
    if path not in paths:
        paths.append(path)
    for entry in entries:
        name, element = _entry_name_and_element(entry)
        if name:
            fields.setdefault(
                name,
                FieldMetadata(
                    name=name,
                    type=_element_type(element),
                    discovery_path=path,
                ),
            )
        _collect_fields(element, path, fields, paths, visited)


def _entry_name_and_element(entry: Any) -> tuple[str | None, Any]:
    if isinstance(entry, tuple) and len(entry) >= 2:
        return str(entry[0]), entry[1]
    name = getattr(entry, "name", None) or _local_name(getattr(entry, "qname", None))
    return str(name) if name else None, entry


def _has_model_fields(zeep_type: Any) -> bool:
    fields, _paths = _discover_fields(zeep_type)
    return bool(fields)


def _element_type(element: Any) -> str:
    element_type = getattr(element, "type", None)
    return str(element_type if element_type is not None else type(element).__name__)


def _local_name(value: Any) -> str | None:
    if value is None:
        return None
    localname = getattr(value, "localname", None)
    if localname:
        return str(localname)
    text = str(value)
    if "}" in text:
        return text.rsplit("}", 1)[-1]
    if ":" in text:
        return text.rsplit(":", 1)[-1]
    return text or None


def _reject_url_credentials(url: str) -> None:
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        raise SystemExit("WSDL URL must not contain embedded credentials.")


def _print_models(models: list[str]) -> None:
    print("Available complex/query models:")
    for model in models:
        print(f"- {model}")


def _print_model(metadata: ModelMetadata) -> None:
    print(f"Model:\n{metadata.model}\n")
    print(f"Factory type:\n{metadata.factory_type}\n")
    print(f"Underlying Zeep type:\n{metadata.underlying_zeep_type}\n")
    print("Supported fields:")
    for field in metadata.fields:
        print(f"- {field.name}")
    print("\nDiscovery path:")
    for path in metadata.discovery_path:
        print(f"- {path.removeprefix('type.')}")


if __name__ == "__main__":
    raise SystemExit(main())
