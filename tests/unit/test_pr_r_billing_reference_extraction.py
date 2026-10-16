"""PR R: BillingReference extraction at read time, and source-evidence replay compatibility."""

from __future__ import annotations

from dataclasses import fields
from datetime import date

import pytest

from app.application.workbench.evidence import ReviewSourceInvoiceEvidence
from app.domain.invoice import parse_ubl_invoice
from app.domain.invoice.billing_reference import extract_billing_references, is_gib_invoice_number
from app.domain.invoice.dto import Header
from app.domain.invoice.exceptions import InvalidInvoiceXmlError, UnsupportedInvoiceXmlError
from app.persistence.workbench_review_source_invoice_reader import (
    review_source_invoice_evidence_fingerprint,
    serialize_review_source_invoice_evidence,
)
from tests.unit.pr_r_fixtures import (
    REFERENCE_NUMBER,
    RETURN_UUID,
    SECOND_REFERENCE_NUMBER,
    return_ubl,
)


def test_extracts_single_reference_with_issue_date_and_type_code() -> None:
    extraction = extract_billing_references(return_ubl())

    assert extraction.invoice_uuid == RETURN_UUID
    (reference,) = extraction.references
    assert reference.position == 1
    assert reference.invoice_number == REFERENCE_NUMBER
    assert reference.issue_date == date(2026, 9, 10)
    assert reference.issue_date_present is True
    assert reference.document_type_code == "IADE"
    assert reference.has_gib_invoice_number is True


def test_extracts_multiple_references_in_document_order() -> None:
    xml = return_ubl(
        references=((SECOND_REFERENCE_NUMBER, "2026-09-11", "IADE"), (REFERENCE_NUMBER, "2026-09-10", None))
    )

    references = extract_billing_references(xml).references

    assert [(item.position, item.invoice_number) for item in references] == [
        (1, SECOND_REFERENCE_NUMBER),
        (2, REFERENCE_NUMBER),
    ]
    assert references[1].document_type_code is None


def test_no_billing_reference_yields_empty_tuple() -> None:
    assert extract_billing_references(return_ubl(references=())).references == ()


def test_values_are_trimmed_but_otherwise_verbatim() -> None:
    xml = return_ubl(references=((f"  {REFERENCE_NUMBER.lower()}  ", " 2026-09-10 ", None),))

    (reference,) = extract_billing_references(xml).references

    assert reference.invoice_number == REFERENCE_NUMBER.lower()
    assert reference.has_gib_invoice_number is False
    assert reference.issue_date == date(2026, 9, 10)


def test_missing_and_invalid_parts_are_reported_not_guessed() -> None:
    xml = return_ubl(references=((None, None, None), (REFERENCE_NUMBER, "10.09.2026", None)))

    missing, invalid_date = extract_billing_references(xml).references

    assert missing.invoice_number is None
    assert missing.issue_date_present is False
    assert invalid_date.issue_date is None
    assert invalid_date.issue_date_present is True


def test_free_text_and_additional_references_are_never_extracted() -> None:
    xml = return_ubl(
        references=(),
        note=f"Iade: {REFERENCE_NUMBER}",
        line_text=f"Return of {REFERENCE_NUMBER}",
        additional_reference_id=REFERENCE_NUMBER,
    )

    assert extract_billing_references(xml).references == ()


def test_malformed_xml_is_rejected() -> None:
    with pytest.raises(InvalidInvoiceXmlError):
        extract_billing_references(b"<Invoice><unclosed>")


def test_doctype_and_entities_are_rejected() -> None:
    xml = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "x">]>' + return_ubl()
    with pytest.raises(InvalidInvoiceXmlError):
        extract_billing_references(xml)


def test_non_invoice_root_is_rejected() -> None:
    with pytest.raises(UnsupportedInvoiceXmlError):
        extract_billing_references(b'<CreditNote xmlns="urn:x"/>')


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (REFERENCE_NUMBER, True),
        ("AB12026000000001", True),
        (REFERENCE_NUMBER.lower(), False),
        ("TST202600000001", False),
        ("TST20260000000011", False),
        (RETURN_UUID, False),
        ("", False),
        (None, False),
    ],
)
def test_gib_invoice_number_format(value: str | None, expected: bool) -> None:
    assert is_gib_invoice_number(value) is expected


# ---------------------------------------------------------------- replay compatibility


def test_canonical_invoice_model_is_not_extended() -> None:
    assert {field.name for field in fields(Header)} == {
        "invoice_number",
        "invoice_uuid",
        "ettn",
        "invoice_type",
        "profile_id",
        "issue_date",
        "issue_time",
        "currency_code",
        "exchange_rate",
        "notes",
    }


def test_billing_reference_does_not_change_persisted_source_evidence() -> None:
    """Stored evidence is fingerprinted on replay; a BillingReference must not alter it."""

    with_reference = _evidence(return_ubl())
    without_reference = _evidence(return_ubl(references=()))

    assert serialize_review_source_invoice_evidence(with_reference) == serialize_review_source_invoice_evidence(
        without_reference
    )
    assert review_source_invoice_evidence_fingerprint(with_reference) == review_source_invoice_evidence_fingerprint(
        without_reference
    )


def _evidence(xml: bytes) -> ReviewSourceInvoiceEvidence:
    invoice = parse_ubl_invoice(xml)
    return ReviewSourceInvoiceEvidence(
        review_id="review:pr-r-replay",
        company_id=7,
        review_version=1,
        source_invoice_id=invoice.header.ettn or invoice.header.invoice_uuid,
        invoice=invoice,
    )
