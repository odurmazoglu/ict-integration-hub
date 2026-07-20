from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.core.config import Settings
from app.models.invoice_document import InvoiceDocument
from app.models.uyumsoft_invoice import UyumsoftInvoiceMetadata
from app.schemas.uyumsoft_invoices import UyumsoftInvoiceListRequest, UyumsoftInvoiceSummary
from app.services.document_service import (
    DOCUMENT_TYPE_UBL_XML,
    DocumentConflictError,
    DocumentDownloadError,
    InvoiceDocumentService,
)
from app.services.document_storage import DocumentStorage, DocumentStorageError
from app.services.invoice_persistence import InvoicePersistenceService, build_invoice_identity

APPROVED_UYUMSOFT_TEST_HOSTS = frozenset({"efatura-test.uyumsoft.com.tr"})
FORBIDDEN_PROVIDER_OPERATIONS = (
    "SetInvoicesTaken",
    "SendInvoice",
    "CancelInvoice",
    "Cancel*",
    "RetrySendInvoices",
    "MoveToDraftStatus",
)
READ_ONLY_OPERATIONS_VALIDATED = (
    "TestConnection",
    "WhoAmI",
    "GetInboxInvoiceList",
    "GetInboxInvoiceData",
)
DATASET_SCENARIOS = (
    "standard_single_line_invoice",
    "multi_line_invoice",
    "foreign_currency_invoice",
    "discount_or_multiple_tax_rates_invoice",
    "likely_missing_or_ambiguous_odoo_resolution_invoice",
)
PLACEHOLDER_VALUES = frozenset({"", "change-me", "example", "placeholder", "todo"})


@dataclass(frozen=True)
class UyumsoftTestValidationRequest:
    from_date: datetime
    to_date: datetime
    limit: int = 5


@dataclass(frozen=True)
class UyumsoftTestValidationService:
    settings: Settings
    client: UyumsoftSoapClient
    session: Session
    storage: DocumentStorage

    def validate(self, request: UyumsoftTestValidationRequest) -> dict[str, Any]:
        target_host = _host(self.settings.uyumsoft_wsdl_url)
        configuration_failures = _configuration_failures(self.settings, target_host, request)
        report = _initial_report(self.settings, target_host, configuration_failures)
        if configuration_failures:
            return report

        if not self._validate_wsdl(report):
            return _finalize_report(report)
        if not self._validate_authentication(report):
            return _finalize_report(report)

        invoices = self._list_invoices(request, report)
        if not invoices:
            report["blockers_for_parser_validation"].append("No incoming Uyumsoft test invoices were returned.")
            return _finalize_report(report)

        collected: dict[str, dict[str, Any]] = {}
        inspected = 0
        for invoice in invoices[: request.limit]:
            inspected += 1
            record = self._persist_invoice_metadata(invoice, report)
            if record is None:
                continue
            item = self._download_and_verify_document(record, report)
            if item is None:
                continue
            content = self.storage.read(item.storage_key)
            scenario_names = _classify_scenarios(content, invoice)
            for scenario_name in scenario_names:
                collected.setdefault(
                    scenario_name,
                    {
                        "invoice_metadata_id": record.id,
                        "document_id": item.document_id,
                        "content_hash_sha256": item.content_hash_sha256,
                        "content_size_bytes": item.content_size_bytes,
                        "storage_backend": item.storage_backend,
                        "document_status": item.status,
                    },
                )
            if len(collected) == len(DATASET_SCENARIOS):
                break

        report["records_inspected"] = inspected
        report["collected_dataset_scenarios"] = collected
        report["missing_dataset_scenarios"] = [scenario for scenario in DATASET_SCENARIOS if scenario not in collected]
        if report["missing_dataset_scenarios"]:
            report["blockers_for_parser_validation"].append(
                "One or more representative UBL dataset scenarios were not found."
            )
        return _finalize_report(report)

    def _validate_wsdl(self, report: dict[str, Any]) -> bool:
        try:
            operations = self.client.inspect_wsdl()
        except (ConnectorTimeoutError, ConnectorError) as exc:
            _record_connector_failure(report, "wsdl_reachability", exc)
            return False
        report["wsdl_reachability"] = {
            "status": "ok",
            "read_only_operations_available": [
                operation
                for operation in READ_ONLY_OPERATIONS_VALIDATED
                if operation in operations.read_only_operations
            ],
        }
        return True

    def _validate_authentication(self, report: dict[str, Any]) -> bool:
        try:
            self.client.test_connection()
            _record_validated_operation(report, "TestConnection")
            self.client.who_am_i()
            _record_validated_operation(report, "WhoAmI")
        except (ConnectorTimeoutError, ConnectorError) as exc:
            _record_connector_failure(report, "authentication", exc)
            return False
        report["authentication"] = {"status": "ok"}
        return True

    def _list_invoices(
        self,
        request: UyumsoftTestValidationRequest,
        report: dict[str, Any],
    ) -> list[UyumsoftInvoiceSummary]:
        list_request = UyumsoftInvoiceListRequest(
            from_date=request.from_date,
            to_date=request.to_date,
            page=1,
            page_size=min(request.limit, 100),
        )
        try:
            response = self.client.list_inbox_invoices(list_request)
            _record_validated_operation(report, "GetInboxInvoiceList")
        except (ConnectorTimeoutError, ConnectorError) as exc:
            _record_connector_failure(report, "invoice_listing", exc)
            return []
        report["invoice_listing"] = {
            "status": "empty" if not response.invoices else "ok",
            "direction": response.direction,
            "page": response.page,
            "page_size": response.page_size,
            "returned_count": len(response.invoices),
            "total_count": response.total_count,
        }
        return response.invoices

    def _persist_invoice_metadata(
        self,
        invoice: UyumsoftInvoiceSummary,
        report: dict[str, Any],
    ) -> UyumsoftInvoiceMetadata | None:
        try:
            InvoicePersistenceService(self.session).persist_invoice(invoice)
            record = self._find_invoice_record(invoice)
        except Exception as exc:
            report["document_persistence"] = {"status": "failed", "message": _safe_message(exc)}
            report["blockers_for_parser_validation"].append("Invoice metadata persistence failed.")
            return None
        if record is None:
            report["document_persistence"] = {
                "status": "failed",
                "message": "Persisted invoice metadata was not found.",
            }
            report["blockers_for_parser_validation"].append("Invoice metadata persistence could not be verified.")
            return None
        report["document_persistence"] = {"status": "ok"}
        return record

    def _download_and_verify_document(
        self,
        record: UyumsoftInvoiceMetadata,
        report: dict[str, Any],
    ) -> Any | None:
        try:
            result = InvoiceDocumentService(
                session=self.session,
                client=self.client,
                storage=self.storage,
            ).download_documents(invoice_ids=[record.id], document_type=DOCUMENT_TYPE_UBL_XML)
        except DocumentConflictError as exc:
            report["detail_retrieval"] = {"status": "failed", "message": exc.safe_message}
            report["ubl_download"] = {"status": "failed", "message": exc.safe_message}
            report["blockers_for_parser_validation"].append("A conflicting UBL document already exists locally.")
            return None
        except (DocumentDownloadError, DocumentStorageError, ConnectorTimeoutError, ConnectorError, OSError) as exc:
            message = _safe_message(exc)
            _record_document_failure(report, message)
            return None

        item = result.items[0]
        report["detail_retrieval"] = {"status": "ok"}
        report["ubl_download"] = {"status": "ok"}
        _record_validated_operation(report, "GetInboxInvoiceData")
        report["document_persistence"] = {
            "status": "ok",
            "last_document_status": item.status,
        }
        document = self.session.get(InvoiceDocument, item.document_id)
        if document is None:
            report["sha256_verification"] = {
                "status": "failed",
                "message": "Persisted document metadata was not found.",
            }
            report["blockers_for_parser_validation"].append("Document metadata persistence could not be verified.")
            return None
        content = self.storage.read(item.storage_key)
        digest = hashlib.sha256(content).hexdigest()
        metadata_ok = (
            digest == item.content_hash_sha256
            and digest == document.content_hash_sha256
            and len(content) == item.content_size_bytes
            and document.content_size_bytes == item.content_size_bytes
            and document.mime_type == "application/xml"
        )
        if not metadata_ok:
            report["sha256_verification"] = {"status": "failed", "message": "Persisted SHA-256 or metadata mismatch."}
            report["blockers_for_parser_validation"].append("Persisted document hash or metadata verification failed.")
            return None
        report["sha256_verification"] = {"status": "ok"}
        return item

    def _find_invoice_record(self, invoice: UyumsoftInvoiceSummary) -> UyumsoftInvoiceMetadata | None:
        identity = build_invoice_identity(invoice)
        if invoice.ettn:
            record = self.session.scalar(
                select(UyumsoftInvoiceMetadata).where(
                    UyumsoftInvoiceMetadata.provider == "uyumsoft",
                    UyumsoftInvoiceMetadata.direction == invoice.direction,
                    UyumsoftInvoiceMetadata.ettn == invoice.ettn.strip(),
                )
            )
            if record is not None:
                return record
        return self.session.scalar(
            select(UyumsoftInvoiceMetadata).where(
                UyumsoftInvoiceMetadata.provider == "uyumsoft",
                UyumsoftInvoiceMetadata.direction == invoice.direction,
                UyumsoftInvoiceMetadata.identity_key == identity.key,
            )
        )


def default_validation_request(
    *, limit: int, from_date: datetime | None, to_date: datetime | None
) -> UyumsoftTestValidationRequest:
    effective_to = _aware_datetime(to_date or datetime.now(tz=UTC))
    effective_from = _aware_datetime(from_date or effective_to - timedelta(days=7))
    return UyumsoftTestValidationRequest(from_date=effective_from, to_date=effective_to, limit=limit)


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_dumps(report, pretty=True), encoding="utf-8")


def _initial_report(settings: Settings, target_host: str, configuration_failures: list[str]) -> dict[str, Any]:
    return {
        "environment": settings.app_env,
        "target_host": target_host,
        "wsdl_reachability": {"status": "not_run" if configuration_failures else "pending"},
        "authentication": {"status": "not_run" if configuration_failures else "pending"},
        "invoice_listing": {"status": "not_run" if configuration_failures else "pending"},
        "records_inspected": 0,
        "detail_retrieval": {"status": "not_run" if configuration_failures else "pending"},
        "ubl_download": {"status": "not_run" if configuration_failures else "pending"},
        "document_persistence": {"status": "not_run" if configuration_failures else "pending"},
        "sha256_verification": {"status": "not_run" if configuration_failures else "pending"},
        "collected_dataset_scenarios": {},
        "missing_dataset_scenarios": list(DATASET_SCENARIOS),
        "permission_failures": [],
        "configuration_failures": configuration_failures,
        "blockers_for_parser_validation": list(configuration_failures),
        "read_only_operations_planned": list(READ_ONLY_OPERATIONS_VALIDATED),
        "read_only_operations_validated": [],
        "no_provider_state_change_attempted": True,
        "forbidden_provider_operations_not_attempted": list(FORBIDDEN_PROVIDER_OPERATIONS),
        "overall_status": "failed" if configuration_failures else "pending",
    }


def _configuration_failures(
    settings: Settings,
    target_host: str,
    request: UyumsoftTestValidationRequest,
) -> list[str]:
    failures: list[str] = []
    if settings.app_env == "production":
        failures.append("APP_ENV=production is not allowed for Uyumsoft test validation.")
    if settings.uyumsoft_environment != "test":
        failures.append("UYUMSOFT_ENVIRONMENT must be test for this validation.")
    if target_host not in APPROVED_UYUMSOFT_TEST_HOSTS:
        failures.append("UYUMSOFT_TEST_WSDL_URL host is not approved for test validation.")
    if target_host == _host(settings.uyumsoft_prod_wsdl_url):
        failures.append("UYUMSOFT_TEST_WSDL_URL must not point to the production host.")
    if _is_placeholder(settings.uyumsoft_username):
        failures.append("UYUMSOFT_USERNAME must be configured with test credentials.")
    if _is_placeholder(settings.uyumsoft_password.get_secret_value()):
        failures.append("UYUMSOFT_PASSWORD must be configured with test credentials.")
    if request.from_date > request.to_date:
        failures.append("from_date must be before or equal to to_date.")
    if request.limit < 1 or request.limit > 100:
        failures.append("limit must be between 1 and 100.")
    return failures


def _finalize_report(report: dict[str, Any]) -> dict[str, Any]:
    report["overall_status"] = (
        "ok"
        if not report["configuration_failures"]
        and not report["permission_failures"]
        and not report["blockers_for_parser_validation"]
        else "failed"
    )
    return report


def _record_connector_failure(report: dict[str, Any], key: str, exc: ConnectorError | ConnectorTimeoutError) -> None:
    status = _failure_status(exc)
    safe_message = exc.safe_message
    report[key] = {"status": status, "message": safe_message}
    if status == "permission_failure":
        report["permission_failures"].append({"target": key, "message": safe_message})
    report["blockers_for_parser_validation"].append(f"{key} failed.")


def _record_document_failure(report: dict[str, Any], message: str) -> None:
    report["detail_retrieval"] = {"status": "failed", "message": message}
    report["ubl_download"] = {"status": "failed", "message": message}
    report["blockers_for_parser_validation"].append("UBL document retrieval or persistence failed.")


def _record_validated_operation(report: dict[str, Any], operation: str) -> None:
    validated = report["read_only_operations_validated"]
    if operation not in validated:
        validated.append(operation)


def _failure_status(exc: Exception) -> str:
    message = _safe_message(exc)
    if "HTTP 401" in message or "HTTP 403" in message or "authorization" in message.lower():
        return "permission_failure"
    if isinstance(exc, ConnectorTimeoutError):
        return "timeout"
    return "failed"


def _classify_scenarios(content: bytes, invoice: UyumsoftInvoiceSummary) -> list[str]:
    root = _parse_xml(content)
    if root is None:
        return []
    scenarios: list[str] = []
    line_count = _count_elements(root, "InvoiceLine")
    if line_count == 1:
        scenarios.append("standard_single_line_invoice")
    if line_count > 1:
        scenarios.append("multi_line_invoice")
    if (invoice.currency or _first_text(root, "DocumentCurrencyCode") or "").upper() not in {"", "TRY"}:
        scenarios.append("foreign_currency_invoice")
    tax_rates = set(_texts(root, "Percent"))
    if _count_elements(root, "AllowanceCharge") > 0 or len(tax_rates) > 1:
        scenarios.append("discount_or_multiple_tax_rates_invoice")
    if _likely_missing_or_ambiguous_resolution(root, invoice):
        scenarios.append("likely_missing_or_ambiguous_odoo_resolution_invoice")
    return scenarios


def _likely_missing_or_ambiguous_resolution(root: ElementTree.Element, invoice: UyumsoftInvoiceSummary) -> bool:
    product_names = [text.strip().lower() for text in _invoice_line_item_names(root) if text.strip()]
    has_duplicate_product_names = len(product_names) != len(set(product_names))
    has_product_code = any(text.strip() for text in _seller_item_ids(root))
    return has_duplicate_product_names or not has_product_code or not invoice.tax_number


def _parse_xml(content: bytes) -> ElementTree.Element | None:
    try:
        return ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return None


def _count_elements(root: ElementTree.Element, local_name: str) -> int:
    return sum(1 for element in root.iter() if _local_name(element.tag) == local_name)


def _texts(root: ElementTree.Element, local_name: str) -> list[str]:
    return [element.text or "" for element in root.iter() if _local_name(element.tag) == local_name]


def _first_text(root: ElementTree.Element, local_name: str) -> str | None:
    for text in _texts(root, local_name):
        stripped = text.strip()
        if stripped:
            return stripped
    return None


def _invoice_line_item_names(root: ElementTree.Element) -> list[str]:
    names: list[str] = []
    for line in _descendants(root, "InvoiceLine"):
        for item in _direct_children(line, "Item"):
            names.extend(text for name in _direct_children(item, "Name") if (text := name.text or ""))
    return names


def _seller_item_ids(root: ElementTree.Element) -> list[str]:
    ids: list[str] = []
    for line in _descendants(root, "InvoiceLine"):
        for item in _direct_children(line, "Item"):
            for seller_identification in _direct_children(item, "SellersItemIdentification"):
                ids.extend(text for node in _direct_children(seller_identification, "ID") if (text := node.text or ""))
    return ids


def _descendants(root: ElementTree.Element, local_name: str) -> list[ElementTree.Element]:
    return [element for element in root.iter() if _local_name(element.tag) == local_name]


def _direct_children(root: ElementTree.Element, local_name: str) -> list[ElementTree.Element]:
    return [element for element in list(root) if _local_name(element.tag) == local_name]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _host(value: object) -> str:
    return (urlparse(str(value)).hostname or "").lower()


def _is_placeholder(value: str) -> bool:
    return value.strip().lower() in PLACEHOLDER_VALUES


def _safe_message(exc: Exception) -> str:
    safe_message = getattr(exc, "safe_message", None)
    if isinstance(safe_message, str) and safe_message.strip():
        return safe_message.strip()[:500]
    return exc.__class__.__name__


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _json_dumps(report: dict[str, Any], *, pretty: bool) -> str:
    import json

    return json.dumps(report, indent=2 if pretty else None, sort_keys=True, default=str)
