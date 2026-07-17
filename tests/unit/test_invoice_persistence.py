from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.models.uyumsoft_invoice import UyumsoftInvoiceMetadata
from app.schemas.uyumsoft_invoices import UyumsoftInvoiceSummary
from app.services.invoice_persistence import InvoicePersistenceService, build_invoice_identity


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


def test_first_insert_persists_normalized_invoice_metadata(session: Session) -> None:
    seen_at = datetime(2026, 7, 17, 12, tzinfo=UTC)
    result = InvoicePersistenceService(session).persist_invoices([_invoice()], seen_at=seen_at)

    record = session.scalar(select(UyumsoftInvoiceMetadata))
    assert result.created == 1
    assert record is not None
    assert record.provider == "uyumsoft"
    assert record.direction == "Inbox"
    assert record.ettn == "ettn-1"
    assert record.identity_strategy == "ettn"
    assert record.sender_name == "Sender A"
    assert record.sender_tax_number == "1111111111"
    assert record.receiver_name == "Receiver B"
    assert record.currency == "TRY"
    assert record.total_amount == Decimal("100.25")
    assert record.first_seen_at == seen_at
    assert record.last_seen_at == seen_at


def test_repeated_sync_does_not_create_duplicate_and_preserves_first_seen_at(session: Session) -> None:
    service = InvoicePersistenceService(session)
    first_seen = datetime(2026, 7, 17, 12, tzinfo=UTC)
    second_seen = first_seen + timedelta(hours=1)

    first = service.persist_invoices([_invoice()], seen_at=first_seen)
    second = service.persist_invoices([_invoice()], seen_at=second_seen)

    records = session.scalars(select(UyumsoftInvoiceMetadata)).all()
    assert first.created == 1
    assert second.skipped == 1
    assert len(records) == 1
    assert records[0].first_seen_at == first_seen
    assert records[0].last_seen_at == second_seen


def test_metadata_update_changes_existing_record(session: Session) -> None:
    service = InvoicePersistenceService(session)
    service.persist_invoices([_invoice(status="NEW")])

    result = service.persist_invoices([_invoice(status="APPROVED", total_amount=Decimal("125.00"))])

    record = session.scalar(select(UyumsoftInvoiceMetadata))
    assert result.updated == 1
    assert record is not None
    assert record.provider_status == "APPROVED"
    assert record.total_amount == Decimal("125.00")


def test_database_constraint_prevents_duplicate_ettn(session: Session) -> None:
    service = InvoicePersistenceService(session)
    service.persist_invoices([_invoice()])
    first = session.scalar(select(UyumsoftInvoiceMetadata))
    assert first is not None

    duplicate = UyumsoftInvoiceMetadata(
        provider=first.provider,
        direction=first.direction,
        provider_invoice_id="other",
        ettn=first.ettn,
        identity_key="ettn:other",
        identity_strategy="ettn",
        raw_metadata={},
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.flush()


def test_missing_ettn_uses_deterministic_fallback_identity(session: Session) -> None:
    invoice = _invoice(ettn=None, provider_invoice_id="provider-1")
    identity = build_invoice_identity(invoice)

    first = InvoicePersistenceService(session).persist_invoices([invoice])
    second = InvoicePersistenceService(session).persist_invoices([invoice])

    records = session.scalars(select(UyumsoftInvoiceMetadata)).all()
    assert identity.strategy == "fallback_v1"
    assert identity.key == build_invoice_identity(invoice).key
    assert first.created == 1
    assert second.skipped == 1
    assert len(records) == 1
    assert records[0].identity_key == identity.key


def test_database_constraint_prevents_concurrent_fallback_duplicates(session: Session) -> None:
    invoice = _invoice(ettn=None, provider_invoice_id="provider-1")
    service = InvoicePersistenceService(session)
    service.persist_invoices([invoice])
    first = session.scalar(select(UyumsoftInvoiceMetadata))
    assert first is not None

    duplicate = UyumsoftInvoiceMetadata(
        provider=first.provider,
        direction=first.direction,
        provider_invoice_id="provider-1",
        ettn=None,
        identity_key=first.identity_key,
        identity_strategy=first.identity_strategy,
        raw_metadata={},
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.flush()


def test_inbox_and_outbox_with_same_ettn_are_separate_identities(session: Session) -> None:
    result = InvoicePersistenceService(session).persist_invoices(
        [_invoice(direction="Inbox"), _invoice(direction="Outbox")]
    )

    records = session.scalars(select(UyumsoftInvoiceMetadata)).all()
    assert result.created == 2
    assert {record.direction for record in records} == {"Inbox", "Outbox"}


def test_decimal_and_timezone_aware_datetime_are_preserved(session: Session) -> None:
    invoice_date = datetime(2026, 7, 17, 9, 30, tzinfo=UTC)
    InvoicePersistenceService(session).persist_invoices(
        [_invoice(invoice_date=invoice_date, total_amount=Decimal("1234.56"))]
    )

    record = session.scalar(select(UyumsoftInvoiceMetadata))
    assert record is not None
    assert record.invoice_date == invoice_date
    assert record.total_amount == Decimal("1234.56")
    assert record.raw_metadata["invoice_date"] == invoice_date.isoformat()
    assert record.raw_metadata["total_amount"] == "1234.56"


def _invoice(
    *,
    direction: str = "Inbox",
    provider_invoice_id: str = "provider-invoice-1",
    ettn: str | None = "ettn-1",
    status: str = "NEW",
    invoice_date: datetime | None = None,
    total_amount: Decimal = Decimal("100.25"),
) -> UyumsoftInvoiceSummary:
    return UyumsoftInvoiceSummary(
        invoice_id=provider_invoice_id,
        ettn=ettn,
        invoice_number="INV-1",
        invoice_date=invoice_date or datetime(2026, 7, 17, 9, tzinfo=UTC),
        sender="Sender A",
        receiver="Receiver B",
        tax_number="1111111111",
        currency="TRY",
        total_amount=total_amount,
        direction=direction,
        status=status,
        extra_fields={"TargetTcknVkn": "2222222222"},
    )
