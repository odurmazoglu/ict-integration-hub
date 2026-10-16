"""PR T W1 security fixes.

R1 -- document storage errors are classified: an unusable key or a missing file under a
verified, populated root is a deterministic ``SOURCE_DOCUMENT_UNVERIFIED``; a missing,
empty (unmounted) or unreadable root, permission denied and any other I/O failure is a
retryable outage that never persists a blocking reason.

R2 -- ``CustomerInvoiceExecutionStrategy`` has its own source-tax check: it re-reads and
verifies the original stored UBL on every attempt, before any ERP write, independent of
the Vendor Bill step, decision acceptance and persisted billing evidence. All synthetic.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from app.application.execution import ExecutionMode
from app.application.execution.contracts import ExecutionStepStatus
from app.application.execution.customer_invoice_strategy import CustomerInvoiceExecutionStrategy
from app.application.execution.exceptions import ExecutionSourceInvoiceError
from app.application.execution.vendor_bill_strategy import (
    SOURCE_TAX_BLOCKED_ERROR_CODE,
    SOURCE_TAX_UNAVAILABLE_ERROR_CODE,
)
from app.application.source_tax import (
    SourceDocumentIssue,
    SourceDocumentUnreadableError,
    SourceTaxAssessment,
    SourceTaxFactsUnavailableError,
)
from app.application.source_tax.reader import SourceTaxSafetyCheck, StoredSourceTaxFactsReader
from app.billing import CustomerInvoiceBuilder
from app.composition.source_tax import StoredSourceTaxDocumentReader
from app.domain.invoice.tax_facts import extract_source_tax_facts
from app.services.document_storage import (
    DocumentNotFoundError,
    DocumentStorageError,
    DocumentStorageUnavailableError,
    InvalidDocumentStorageKeyError,
    LocalDocumentStorage,
)
from tests.unit.pr_t_w1_fixtures import LineSpec, TaxSpec, sha256, synthetic_ubl
from tests.unit.source_tax_support import FixedSourceTaxFactsReader
from tests.unit.test_customer_invoice_execution_strategy import (
    RecordingCustomerInvoiceWriter,
    StaticSourceInvoiceReader,
    _instruction,
    _request,
)

KEY = "uyumsoft/inbox/1/ubl_xml/document.xml"


def _populated_root(tmp_path: Path) -> Path:
    root = tmp_path / "documents"
    (root / "uyumsoft" / "inbox" / "0").mkdir(parents=True)
    (root / "uyumsoft" / "inbox" / "0" / "other.xml").write_bytes(b"<other/>")
    return root


def _store(root: Path, key: str, content: bytes) -> None:
    target = root / Path(*PurePosixPath(key).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


# ===================================================================== R1: storage layer


def test_existing_document_is_read(tmp_path: Path) -> None:
    root = _populated_root(tmp_path)
    _store(root, KEY, b"<Invoice/>")

    assert LocalDocumentStorage(root).read(KEY) == b"<Invoice/>"


def test_missing_file_under_a_verified_root_is_deterministic(tmp_path: Path) -> None:
    with pytest.raises(DocumentNotFoundError):
        LocalDocumentStorage(_populated_root(tmp_path)).read(KEY)


def test_missing_root_is_an_outage(tmp_path: Path) -> None:
    with pytest.raises(DocumentStorageUnavailableError):
        LocalDocumentStorage(tmp_path / "not-mounted").read(KEY)


def test_empty_root_is_an_outage_never_a_missing_document(tmp_path: Path) -> None:
    """An unmounted volume leaves an empty mountpoint; that is never a verdict."""

    empty = tmp_path / "mountpoint"
    empty.mkdir()

    with pytest.raises(DocumentStorageUnavailableError):
        LocalDocumentStorage(empty).read(KEY)


def test_root_that_is_a_file_is_an_outage(tmp_path: Path) -> None:
    root = tmp_path / "not-a-directory"
    root.write_text("x")

    with pytest.raises(DocumentStorageUnavailableError):
        LocalDocumentStorage(root).read(KEY)


def test_unreadable_root_is_an_outage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _populated_root(tmp_path)
    real_access = os.access
    monkeypatch.setattr(os, "access", lambda path, mode: False if Path(path) == root else real_access(path, mode))

    with pytest.raises(DocumentStorageUnavailableError):
        LocalDocumentStorage(root).read(KEY)


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(errno.EACCES, "Permission denied"),
        OSError(errno.EIO, "Input/output error"),
        OSError(errno.ESTALE, "Stale file handle"),
        IsADirectoryError(errno.EISDIR, "Is a directory"),
    ],
    ids=["permission-denied", "eio", "stale-nfs", "is-a-directory"],
)
def test_other_read_failures_are_outages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError) -> None:
    root = _populated_root(tmp_path)
    _store(root, KEY, b"<Invoice/>")

    def fail(self: Path) -> bytes:
        raise error

    monkeypatch.setattr(Path, "read_bytes", fail)

    with pytest.raises(DocumentStorageUnavailableError):
        LocalDocumentStorage(root).read(KEY)


@pytest.mark.parametrize(
    "key",
    ["", ".", "/etc/passwd", "../escape.xml", "uyumsoft/../../escape.xml", "a\x00b.xml"],
    ids=["empty", "dot", "absolute", "parent", "nested-parent", "nul"],
)
def test_unsafe_keys_are_deterministically_invalid(tmp_path: Path, key: str) -> None:
    with pytest.raises(InvalidDocumentStorageKeyError):
        LocalDocumentStorage(_populated_root(tmp_path)).read(key)


def test_symlink_escaping_the_root_is_an_invalid_key(tmp_path: Path) -> None:
    root = _populated_root(tmp_path)
    outside = tmp_path / "outside.xml"
    outside.write_bytes(b"<Invoice/>")
    (root / "uyumsoft" / "link.xml").symlink_to(outside)

    with pytest.raises(InvalidDocumentStorageKeyError):
        LocalDocumentStorage(root).read("uyumsoft/link.xml")


def test_new_errors_stay_document_storage_errors_for_existing_callers(tmp_path: Path) -> None:
    for error in (InvalidDocumentStorageKeyError, DocumentNotFoundError, DocumentStorageUnavailableError):
        assert issubclass(error, DocumentStorageError)
    with pytest.raises(DocumentStorageError):
        LocalDocumentStorage(tmp_path).write("../escape.xml", b"x")


# ===================================================================== R1: source-tax adapter + reader


@pytest.mark.parametrize(
    ("error", "issue"),
    [
        (InvalidDocumentStorageKeyError("bad"), SourceDocumentIssue.STORAGE_KEY_INVALID),
        (DocumentNotFoundError("gone"), SourceDocumentIssue.DOCUMENT_FILE_MISSING),
    ],
)
def test_adapter_maps_deterministic_storage_failures_to_a_verdict(error, issue) -> None:
    storage = SimpleNamespace(read=lambda key: (_ for _ in ()).throw(error))

    with pytest.raises(SourceDocumentUnreadableError) as raised:
        StoredSourceTaxDocumentReader(storage).read(KEY)  # type: ignore[arg-type]

    assert raised.value.issue is issue


@pytest.mark.parametrize(
    "error", [DocumentStorageUnavailableError("outage"), DocumentStorageError("legacy generic failure")]
)
def test_adapter_maps_every_other_storage_failure_to_a_retryable_outage(error) -> None:
    storage = SimpleNamespace(read=lambda key: (_ for _ in ()).throw(error))

    with pytest.raises(SourceTaxFactsUnavailableError):
        StoredSourceTaxDocumentReader(storage).read(KEY)  # type: ignore[arg-type]


def _real_reader(root: Path, *, key: str = KEY, stored_hash: str = "0" * 64) -> StoredSourceTaxFactsReader:
    document = SimpleNamespace(storage_key=key, content_sha256=stored_hash)
    return StoredSourceTaxFactsReader(
        document_locator=SimpleNamespace(find_source_documents=lambda **_: (document,)),
        document_reader=StoredSourceTaxDocumentReader(LocalDocumentStorage(root)),
    )


def _assess(reader: StoredSourceTaxFactsReader) -> SourceTaxAssessment:
    return reader.assess(review_id="review-1", company_id=7, invoice_uuid="7e570000-0000-4000-8000-0000000000a1")


def test_reader_missing_file_with_root_present_is_unverified(tmp_path: Path) -> None:
    assert _assess(_real_reader(_populated_root(tmp_path))).issue is SourceDocumentIssue.DOCUMENT_FILE_MISSING


def test_reader_unsafe_key_is_unverified(tmp_path: Path) -> None:
    assessment = _assess(_real_reader(_populated_root(tmp_path), key="../escape.xml"))

    assert assessment.issue is SourceDocumentIssue.STORAGE_KEY_INVALID


@pytest.mark.parametrize("root_state", ["missing", "empty"])
def test_reader_missing_or_unmounted_root_is_retryable(tmp_path: Path, root_state: str) -> None:
    root = tmp_path / "documents"
    if root_state == "empty":
        root.mkdir()

    with pytest.raises(SourceTaxFactsUnavailableError):
        _assess(_real_reader(root))


def test_reader_permission_denied_and_io_errors_are_retryable(tmp_path: Path, monkeypatch) -> None:
    root = _populated_root(tmp_path)
    _store(root, KEY, b"<Invoice/>")
    for error in (PermissionError(errno.EACCES, "denied"), OSError(errno.EIO, "io")):

        def fail(self: Path, error: OSError = error) -> bytes:
            raise error

        monkeypatch.setattr(Path, "read_bytes", fail)
        with pytest.raises(SourceTaxFactsUnavailableError):
            _assess(_real_reader(root))


def test_reader_clean_document_is_verified(tmp_path: Path) -> None:
    root = _populated_root(tmp_path)
    content = synthetic_ubl()
    _store(root, KEY, content)

    assert _assess(_real_reader(root, stored_hash=sha256(content))).is_verified


# ===================================================================== R2: customer invoice execution


def _verified(**spec) -> SourceTaxAssessment:
    return SourceTaxAssessment.verified(extract_source_tax_facts(synthetic_ubl(**spec)))


def _customer_strategy(check) -> tuple[CustomerInvoiceExecutionStrategy, RecordingCustomerInvoiceWriter]:
    writer = RecordingCustomerInvoiceWriter()
    strategy = CustomerInvoiceExecutionStrategy(
        source_invoice_reader=StaticSourceInvoiceReader(),
        customer_invoice_builder=CustomerInvoiceBuilder(),
        customer_invoice_writer=writer,
        source_tax_safety_check=check,
    )
    return strategy, writer


def _creation_request(mode: ExecutionMode):
    """A creation step carrying a billing instruction -- exactly what persisted (Stage-2)
    billing evidence turns into, including evidence accepted before W1."""

    return _request(customer_invoice_id=None, mode=mode, billing_instruction=_instruction())


def _check(assessment=None, *, error=None):
    reader = FixedSourceTaxFactsReader(assessment, error=error)
    return SourceTaxSafetyCheck(facts_reader=reader), reader


BLOCKING = {
    "withholding-type": _verified(invoice_type="TEVKIFAT"),
    "withholding-line": _verified(lines=(LineSpec(withholding=True),)),
    "non-0015": _verified(lines=(LineSpec(taxes=(TaxSpec(code="4080"),)),)),
    "unverified": SourceTaxAssessment.unverified(SourceDocumentIssue.HASH_MISMATCH),
    "file-missing": SourceTaxAssessment.unverified(SourceDocumentIssue.DOCUMENT_FILE_MISSING),
}


@pytest.mark.parametrize("mode", [ExecutionMode.DRY_RUN, ExecutionMode.EXECUTE])
@pytest.mark.parametrize("name", sorted(BLOCKING))
def test_customer_invoice_is_blocked_before_any_write(mode: ExecutionMode, name: str) -> None:
    check, reader = _check(BLOCKING[name])
    strategy, writer = _customer_strategy(check)

    result = strategy.execute(_creation_request(mode))

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == SOURCE_TAX_BLOCKED_ERROR_CODE
    assert writer.calls == 0
    assert len(reader.calls) == 1


def test_customer_invoice_with_tampered_stored_xml_is_blocked(tmp_path: Path) -> None:
    root = _populated_root(tmp_path)
    original = synthetic_ubl()
    _store(root, KEY, original.replace(b"SYNTHETIC SUPPLIER", b"TAMPERED SUPPLIER"))
    check = SourceTaxSafetyCheck(facts_reader=_real_reader(root, stored_hash=sha256(original)))
    strategy, writer = _customer_strategy(check)

    result = strategy.execute(_creation_request(ExecutionMode.EXECUTE))

    assert result.error_code == SOURCE_TAX_BLOCKED_ERROR_CODE
    assert writer.calls == 0


@pytest.mark.parametrize("mode", [ExecutionMode.DRY_RUN, ExecutionMode.EXECUTE])
def test_customer_invoice_storage_outage_is_a_retryable_failure(mode: ExecutionMode) -> None:
    check, _ = _check(error=SourceTaxFactsUnavailableError("offline"))
    strategy, writer = _customer_strategy(check)

    result = strategy.execute(_creation_request(mode))

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == SOURCE_TAX_UNAVAILABLE_ERROR_CODE
    assert writer.calls == 0


def test_customer_invoice_retry_and_resume_re_verify_every_attempt() -> None:
    check, reader = _check(error=SourceTaxFactsUnavailableError("offline"))
    strategy, writer = _customer_strategy(check)

    first = strategy.execute(_creation_request(ExecutionMode.EXECUTE))  # outage
    reader.error = None
    reader.assessment = BLOCKING["withholding-type"]
    retried = strategy.execute(_creation_request(ExecutionMode.EXECUTE))  # retry: now blocked
    resumed = strategy.execute(_creation_request(ExecutionMode.EXECUTE))  # resume: still blocked

    assert [first.error_code, retried.error_code, resumed.error_code] == [
        SOURCE_TAX_UNAVAILABLE_ERROR_CODE,
        SOURCE_TAX_BLOCKED_ERROR_CODE,
        SOURCE_TAX_BLOCKED_ERROR_CODE,
    ]
    assert len(reader.calls) == 3
    assert writer.calls == 0


def test_clean_customer_invoice_is_unchanged_and_checked_against_the_pinned_invoice() -> None:
    check, reader = _check(_verified())
    strategy, writer = _customer_strategy(check)

    result = strategy.execute(_creation_request(ExecutionMode.EXECUTE))

    assert result.status is ExecutionStepStatus.EXECUTED
    assert writer.calls == 1
    pinned = StaticSourceInvoiceReader().get_source_invoice(review_id="review-1", company_id=7, decision_version=2)
    assert reader.calls == [("review-1", 7, pinned.invoice.header.invoice_uuid)]


def test_customer_invoice_requires_the_check() -> None:
    with pytest.raises(TypeError):
        CustomerInvoiceExecutionStrategy(  # type: ignore[call-arg]
            source_invoice_reader=StaticSourceInvoiceReader(),
            customer_invoice_builder=CustomerInvoiceBuilder(),
            customer_invoice_writer=RecordingCustomerInvoiceWriter(),
        )
    with pytest.raises(ExecutionSourceInvoiceError):
        _customer_strategy(None)


def test_production_composition_wires_the_check_into_every_accounting_strategy() -> None:
    source = Path("app/composition/execution.py").read_text(encoding="utf-8")
    for strategy in (
        "VendorBillExecutionStrategy(",
        "ExistingPurchaseOrderExecutionStrategy(",
        "CustomerInvoiceExecutionStrategy(",
    ):
        start = source.index(strategy)
        block = source[start : source.index("\n    )", start) if strategy.startswith("Vendor") else start + 600]
        assert "source_tax_safety_check=source_tax_safety_check" in block, strategy
