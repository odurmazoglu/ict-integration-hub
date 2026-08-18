from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.application.commands import ImportInvoiceCommand
from app.application.dto import ImportInvoiceResult
from app.domain.invoice import InternalInvoice
from app.erp.models import Company
from app.schemas.uyumsoft_invoices import UyumsoftInvoiceSummary
from app.services.document_service import DocumentDownloadItem, DocumentDownloadResult, DocumentValidationError
from app.services.document_storage import DocumentStorageError
from app.services.uyumsoft_canonical_import import (
    IMPORT_STATUS_ALREADY_IMPORTED,
    IMPORT_STATUS_CANONICAL_IMPORT_FAILED,
    IMPORT_STATUS_COMPANY_RESOLUTION_FAILED,
    IMPORT_STATUS_IMPORTED,
    IMPORT_STATUS_NORMALIZATION_FAILED,
    IMPORT_STATUS_PROVIDER_DOWNLOAD_FAILED,
    IMPORT_STATUS_REVIEW_CREATED,
    IMPORT_STATUS_SKIPPED_DIRECTION,
    ExactCompanyResolver,
    UyumsoftCanonicalInvoiceImporter,
    import_idempotency_key,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ubl"


def test_importer_reuses_existing_ubl_parser_and_passes_internal_invoice_to_import_use_case() -> None:
    use_case = RecordingImportUseCase(
        ImportInvoiceResult(
            success=False,
            invoice_id="11111111-2222-3333-4444-555555555555",
            status="review_required",
            review_required=True,
            review_id="review-1",
        )
    )
    importer = _importer(use_case=use_case)

    outcome = importer.import_invoice(_invoice(), persisted_record=_record())

    assert outcome.status == IMPORT_STATUS_REVIEW_CREATED
    assert use_case.commands
    command = use_case.commands[0]
    assert isinstance(command.invoice, InternalInvoice)
    assert command.invoice.customer.tax_number == "2222222222"
    assert command.company_id == 7
    assert command.dry_run is True


def test_exact_company_resolution_failure_does_not_call_import_use_case() -> None:
    use_case = RecordingImportUseCase(_success_result())
    importer = _importer(use_case=use_case, companies=())

    outcome = importer.import_invoice(_invoice(), persisted_record=_record())

    assert outcome.status == IMPORT_STATUS_COMPANY_RESOLUTION_FAILED
    assert use_case.commands == []


def test_idempotency_key_is_stable_and_company_scoped() -> None:
    invoice = _invoice(ettn="same-ettn")

    first = import_idempotency_key(company_id=7, provider="uyumsoft", invoice=invoice)
    second = import_idempotency_key(company_id=7, provider="uyumsoft", invoice=invoice)
    other_company = import_idempotency_key(company_id=8, provider="uyumsoft", invoice=invoice)

    assert first == second
    assert first == "uyumsoft:company:7:inbox:ettn:same-ettn"
    assert other_company != first


def test_already_imported_canonical_result_is_reported_idempotently() -> None:
    importer = _importer(
        use_case=RecordingImportUseCase(
            ImportInvoiceResult(success=True, invoice_id="same-ettn", status="already_imported")
        )
    )

    outcome = importer.import_invoice(_invoice(ettn="same-ettn"), persisted_record=_record())

    assert outcome.status == IMPORT_STATUS_ALREADY_IMPORTED
    assert outcome.imported_invoice_id == "same-ettn"


def test_successful_non_review_import_is_counted_as_imported() -> None:
    importer = _importer(use_case=RecordingImportUseCase(_success_result()))

    outcome = importer.import_invoice(_invoice(), persisted_record=_record())

    assert outcome.status == IMPORT_STATUS_IMPORTED
    assert outcome.import_status == "dry_run"


def test_malformed_ubl_returns_safe_failure_without_fabricating_internal_invoice() -> None:
    use_case = RecordingImportUseCase(_success_result())
    importer = _importer(use_case=use_case, content=b"<Invoice>")

    outcome = importer.import_invoice(_invoice(), persisted_record=_record())

    assert outcome.status == IMPORT_STATUS_NORMALIZATION_FAILED
    assert outcome.safe_message == "Malformed invoice XML."
    assert use_case.commands == []


def test_provider_download_failure_does_not_fabricate_internal_invoice() -> None:
    use_case = RecordingImportUseCase(_success_result())
    importer = _importer(use_case=use_case, document_service=FailingDocumentService())

    outcome = importer.import_invoice(_invoice(), persisted_record=_record())

    assert outcome.status == IMPORT_STATUS_PROVIDER_DOWNLOAD_FAILED
    assert outcome.safe_message == "Downloaded document is empty or invalid."
    assert use_case.commands == []


def test_batch_continues_when_one_invoice_fails_normalization() -> None:
    use_case = RecordingImportUseCase(_success_result())
    storage = SelectiveStorage({"key-1": _valid_ubl(), "key-2": b"<Invoice>"})
    importer = _importer(use_case=use_case, storage=storage, document_service=SequentialDocumentService())
    first = _invoice(ettn="first", invoice_id="first", invoice_number="first")
    second = _invoice(ettn="second", invoice_id="second", invoice_number="second")

    result = importer.import_invoices(
        [first, second],
        persisted_records={"ettn:first": _record(record_id=1), "ettn:second": _record(record_id=2)},
    )

    assert [outcome.status for outcome in result.outcomes] == [
        IMPORT_STATUS_IMPORTED,
        IMPORT_STATUS_NORMALIZATION_FAILED,
    ]
    assert len(use_case.commands) == 1


def test_outbox_invoice_is_not_imported_as_supplier_invoice() -> None:
    use_case = RecordingImportUseCase(_success_result())
    importer = _importer(use_case=use_case)

    outcome = importer.import_invoice(_invoice(direction="Outbox"), persisted_record=_record(direction="Outbox"))

    assert outcome.status == IMPORT_STATUS_SKIPPED_DIRECTION
    assert use_case.commands == []


def test_canonical_import_failure_uses_safe_error_surface() -> None:
    importer = _importer(use_case=FailingImportUseCase())

    outcome = importer.import_invoice(_invoice(), persisted_record=_record())

    assert outcome.status == IMPORT_STATUS_CANONICAL_IMPORT_FAILED
    assert outcome.safe_message == "Canonical invoice import failed."


def test_no_duplicate_parser_or_projection_logic_in_uyumsoft_layer() -> None:
    source = Path("app/services/uyumsoft_canonical_import.py").read_text()
    sync_source = Path("app/services/uyumsoft_invoice_sync.py").read_text()
    connector_source = "\n".join(path.read_text() for path in Path("app/connectors/uyumsoft").rglob("*.py"))

    assert "parse_ubl_invoice" in source
    assert "ElementTree" not in source
    assert "OdooWorkbenchProjectionPublisher" not in source + sync_source + connector_source
    assert "ReviewItemCreationService" not in source + sync_source + connector_source
    assert "create_account_move" not in source + sync_source + connector_source
    assert "SendInvoice" not in source + sync_source
    assert "fuzzy" not in source.lower()
    assert "OpenAI" not in source


def _importer(
    *,
    use_case: object,
    companies: tuple[Company, ...] = (Company(id=7, name="ICT", tax_number="2222222222"),),
    content: bytes | None = None,
    storage: object | None = None,
    document_service: object | None = None,
) -> UyumsoftCanonicalInvoiceImporter:
    return UyumsoftCanonicalInvoiceImporter(
        document_service=document_service or FakeDocumentService(),
        storage=storage or FakeStorage(content or _valid_ubl()),
        company_resolver=ExactCompanyResolver(FakeCompanyRepository(companies)),
        import_use_case_factory=lambda: use_case,  # type: ignore[arg-type]
    )


def _valid_ubl() -> bytes:
    return (FIXTURES / "valid_invoice.xml").read_bytes()


def _invoice(
    *,
    direction: str = "Inbox",
    ettn: str = "11111111-2222-3333-4444-555555555555",
    invoice_id: str = "provider-1",
    invoice_number: str = "SYN202600001",
) -> UyumsoftInvoiceSummary:
    return UyumsoftInvoiceSummary(
        invoice_id=invoice_id,
        ettn=ettn,
        invoice_number=invoice_number,
        invoice_date=datetime(2026, 7, 20, tzinfo=UTC),
        sender="Synthetic Supplier Ltd",
        receiver="Synthetic Customer A.S.",
        tax_number="1111111111",
        currency="TRY",
        total_amount=Decimal("281.75"),
        direction=direction,  # type: ignore[arg-type]
        status="NEW",
    )


@dataclass
class FakeRecord:
    id: int
    direction: str
    identity_key: str


def _record(*, record_id: int = 1, direction: str = "Inbox") -> FakeRecord:
    return FakeRecord(id=record_id, direction=direction, identity_key=f"ettn:{'first' if record_id == 1 else 'second'}")


def _success_result() -> ImportInvoiceResult:
    return ImportInvoiceResult(success=True, invoice_id="invoice-1", status="dry_run")


class RecordingImportUseCase:
    def __init__(self, result: ImportInvoiceResult) -> None:
        self.result = result
        self.commands: list[ImportInvoiceCommand] = []

    async def execute(self, command: ImportInvoiceCommand) -> ImportInvoiceResult:
        self.commands.append(command)
        return self.result


class FailingImportUseCase:
    async def execute(self, command: ImportInvoiceCommand) -> ImportInvoiceResult:
        raise RuntimeError("provider payload hidden")


class FakeCompanyRepository:
    def __init__(self, companies: tuple[Company, ...]) -> None:
        self.companies = companies
        self.tax_number_calls: list[str] = []

    def find_by_tax_number(self, tax_number: str) -> tuple[Company, ...]:
        self.tax_number_calls.append(tax_number)
        return self.companies

    def find_by_id(self, company_id: int) -> Company | None:
        return None

    def find_default(self) -> Company | None:
        return None


class FakeDocumentService:
    def download_documents(self, *, invoice_ids: list[int], document_type: str) -> DocumentDownloadResult:
        return _download_result(invoice_ids[0], "key")


class SequentialDocumentService:
    def download_documents(self, *, invoice_ids: list[int], document_type: str) -> DocumentDownloadResult:
        return _download_result(invoice_ids[0], f"key-{invoice_ids[0]}")


class FailingDocumentService:
    def download_documents(self, *, invoice_ids: list[int], document_type: str) -> DocumentDownloadResult:
        raise DocumentValidationError("Downloaded document is empty or invalid.")


class FakeStorage:
    backend_name = "memory"

    def __init__(self, content: bytes) -> None:
        self.content = content

    def read(self, storage_key: str) -> bytes:
        return self.content

    def write(self, storage_key: str, content: bytes) -> None:
        pass

    def delete(self, storage_key: str) -> None:
        pass


class SelectiveStorage:
    backend_name = "memory"

    def __init__(self, content_by_key: dict[str, bytes]) -> None:
        self.content_by_key = content_by_key

    def read(self, storage_key: str) -> bytes:
        try:
            return self.content_by_key[storage_key]
        except KeyError as exc:
            raise DocumentStorageError("Document storage read failed.") from exc

    def write(self, storage_key: str, content: bytes) -> None:
        pass

    def delete(self, storage_key: str) -> None:
        pass


def _download_result(invoice_id: int, storage_key: str) -> DocumentDownloadResult:
    return DocumentDownloadResult(
        provider="uyumsoft",
        document_type="UBL_XML",
        items=[
            DocumentDownloadItem(
                invoice_id=invoice_id,
                document_id=invoice_id,
                status="existing",
                document_type="UBL_XML",
                storage_backend="memory",
                storage_key=storage_key,
                content_hash_sha256="0" * 64,
                content_size_bytes=10,
            )
        ],
    )
