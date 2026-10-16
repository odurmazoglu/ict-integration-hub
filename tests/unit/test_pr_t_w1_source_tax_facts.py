"""PR T W1: source tax facts extraction, document verification and the reasons they imply.

All documents are synthetic (``tests/unit/pr_t_w1_fixtures.py``).
"""

from __future__ import annotations

import pytest

from app.application.source_tax import (
    SourceDocumentIssue,
    SourceTaxAssessment,
    assess_source_document,
    source_tax_review_reasons,
)
from app.application.workflow import SOURCE_TAX_REASON_SOURCE, ManualReviewReasonCode
from app.domain.invoice import parse_ubl_invoice
from app.domain.invoice.exceptions import InvoiceDomainError
from app.domain.invoice.tax_facts import extract_source_tax_facts
from tests.unit.pr_t_w1_fixtures import (
    PRODUCTION_COMPATIBILITY_SHAPES,
    SYNTHETIC_UUID,
    DocumentSpec,
    LineSpec,
    TaxSpec,
    sha256,
    synthetic_ubl,
)

WITHHOLDING = ManualReviewReasonCode.WITHHOLDING_TAX_NOT_SUPPORTED
UNVERIFIED = ManualReviewReasonCode.SOURCE_DOCUMENT_UNVERIFIED
UNSUPPORTED = ManualReviewReasonCode.UNSUPPORTED_INVOICE_CONTENT


def _assess(content: bytes, *, expected_sha256: str | None = None, uuid: str | None = SYNTHETIC_UUID):
    return assess_source_document(
        content,
        expected_sha256=sha256(content) if expected_sha256 is None else expected_sha256,
        expected_invoice_uuid=uuid,
    )


def _codes(assessment: SourceTaxAssessment) -> tuple[ManualReviewReasonCode, ...]:
    return tuple(reason.code for reason in source_tax_review_reasons(assessment))


# ----------------------------------------------------------------------------- extraction


def test_clean_vat_invoice_has_no_withholding_and_only_vat() -> None:
    facts = extract_source_tax_facts(synthetic_ubl())

    assert facts.invoice_uuid == SYNTHETIC_UUID
    assert facts.invoice_type_code == "SATIS"
    assert facts.tax_type_codes == ("0015",)
    assert not facts.has_withholding
    assert not facts.has_unsupported_tax_content


def test_invoice_level_withholding_is_detected() -> None:
    facts = extract_source_tax_facts(synthetic_ubl(invoice_withholding=True))

    assert facts.has_withholding
    assert (facts.invoice_withholding_total_count, facts.line_withholding_total_count) == (1, 0)


def test_line_level_withholding_is_detected() -> None:
    lines = (LineSpec(), LineSpec(withholding=True), LineSpec(withholding=True))
    facts = extract_source_tax_facts(synthetic_ubl(lines=lines))

    assert facts.has_withholding
    assert (facts.invoice_withholding_total_count, facts.line_withholding_total_count) == (0, 2)


def test_withholding_in_any_namespace_is_detected() -> None:
    facts = extract_source_tax_facts(synthetic_ubl(invoice_withholding=True, withholding_prefix="ext"))

    assert facts.has_withholding
    assert facts.invoice_withholding_total_count == 1


@pytest.mark.parametrize("code", ["TEVKIFAT", "YTBTEVKIFAT", " tevkifat ", "TEVKIFATIADE", "YTBTEVKIFATIADE"])
def test_withholding_invoice_type_codes_are_withholding_without_any_total(code: str) -> None:
    facts = extract_source_tax_facts(synthetic_ubl(invoice_type=code))

    assert facts.invoice_type_code == code.strip().upper()
    assert facts.withholding_invoice_type
    assert facts.has_withholding


@pytest.mark.parametrize("code", ["SATIS", "ISTISNA", "IADE", "IHRACKAYITLI", "OZELMATRAH", "SGK", None])
def test_other_invoice_type_codes_are_not_withholding(code: str | None) -> None:
    assert not extract_source_tax_facts(synthetic_ubl(invoice_type=code)).has_withholding


def test_withholding_subtotal_codes_are_not_counted_as_unsupported_vat_codes() -> None:
    facts = extract_source_tax_facts(synthetic_ubl(invoice_withholding=True, lines=(LineSpec(withholding=True),)))

    assert facts.tax_type_codes == ("0015",)
    assert facts.unsupported_tax_type_codes == ()


@pytest.mark.parametrize("code", ["4080", "0003", "0071", "9015"])
def test_non_vat_tax_type_codes_are_unsupported(code: str) -> None:
    lines = (LineSpec(taxes=(TaxSpec(), TaxSpec(code=code, name="OTHER", amount="5.00"))),)
    facts = extract_source_tax_facts(synthetic_ubl(lines=lines))

    assert facts.unsupported_tax_type_codes == (code,)
    assert facts.has_unsupported_tax_content
    assert not facts.has_withholding


def test_invoice_level_non_vat_code_is_unsupported_even_when_lines_are_vat() -> None:
    facts = extract_source_tax_facts(synthetic_ubl(invoice_taxes=(TaxSpec(), TaxSpec(code="4080"))))

    assert facts.unsupported_tax_type_codes == ("4080",)


def test_subtotal_without_tax_type_code_is_counted_never_assumed_vat() -> None:
    facts = extract_source_tax_facts(synthetic_ubl(lines=(LineSpec(taxes=(TaxSpec(code=None),)),)))

    assert facts.tax_subtotals_without_type_code == 2  # invoice-level copy + line
    assert facts.has_unsupported_tax_content


@pytest.mark.parametrize(
    "content",
    [
        b'<!DOCTYPE Invoice [<!ENTITY x "y">]><Invoice/>',
        synthetic_ubl(prolog='<?xml version="1.0"?><!DOCTYPE Invoice>'),
        b"<Invoice><unclosed></Invoice>",
        b'<Other xmlns="urn:example">x</Other>',
    ],
    ids=["entity", "dtd", "malformed", "not-an-invoice"],
)
def test_unsafe_or_invalid_xml_is_rejected_like_the_canonical_parser(content: bytes) -> None:
    with pytest.raises(InvoiceDomainError):
        extract_source_tax_facts(content)


# ---------------------------------------------------------------- 62-invoice compatibility


@pytest.mark.parametrize("shape", sorted(PRODUCTION_COMPATIBILITY_SHAPES))
def test_production_invoice_shapes_carry_no_source_tax_reason(shape: str) -> None:
    content = synthetic_ubl(PRODUCTION_COMPATIBILITY_SHAPES[shape])
    assessment = _assess(content)

    assert assessment.is_verified
    assert source_tax_review_reasons(assessment) == ()
    # The canonical parser is untouched by W1 and still parses every shape.
    assert parse_ubl_invoice(content).header.invoice_uuid == SYNTHETIC_UUID


# ----------------------------------------------------------------------------- verification


def test_verified_document_yields_facts() -> None:
    assessment = _assess(synthetic_ubl())

    assert assessment.is_verified
    assert assessment.issue is None


def test_hash_comparison_is_case_and_whitespace_tolerant_only() -> None:
    content = synthetic_ubl()

    assert _assess(content, expected_sha256=f" {sha256(content).upper()} ").is_verified


@pytest.mark.parametrize("expected", ["0" * 64, "", "not-a-hash"])
def test_hash_mismatch_is_unverified(expected: str) -> None:
    assessment = _assess(synthetic_ubl(), expected_sha256=expected)

    assert assessment.issue is SourceDocumentIssue.HASH_MISMATCH
    assert assessment.facts is None


def test_hash_is_checked_before_parsing() -> None:
    content = b'<!DOCTYPE Invoice [<!ENTITY x "y">]><Invoice/>'

    assert _assess(content, expected_sha256="0" * 64).issue is SourceDocumentIssue.HASH_MISMATCH
    assert _assess(content).issue is SourceDocumentIssue.DOCUMENT_INVALID


def test_uuid_mismatch_is_unverified() -> None:
    assessment = _assess(synthetic_ubl(), uuid="7e570000-0000-4000-8000-0000000000ff")

    assert assessment.issue is SourceDocumentIssue.UUID_MISMATCH


def test_uuid_comparison_is_exact() -> None:
    assert _assess(synthetic_ubl(), uuid=SYNTHETIC_UUID.upper()).issue is SourceDocumentIssue.UUID_MISMATCH


@pytest.mark.parametrize("expected", [None, "", "  "])
def test_missing_expected_uuid_is_unverified(expected: str | None) -> None:
    assert _assess(synthetic_ubl(), uuid=expected).issue is SourceDocumentIssue.UUID_MISSING


def test_document_without_uuid_is_unverified() -> None:
    content = synthetic_ubl().replace(f"<cbc:UUID>{SYNTHETIC_UUID}</cbc:UUID>".encode(), b"")

    assert _assess(content).issue is SourceDocumentIssue.UUID_MISSING


def test_corrupt_bytes_with_matching_hash_are_invalid() -> None:
    content = synthetic_ubl()[:200]

    assert _assess(content).issue is SourceDocumentIssue.DOCUMENT_INVALID


def test_assessment_carries_exactly_one_of_facts_or_issue() -> None:
    with pytest.raises(ValueError):
        SourceTaxAssessment()
    with pytest.raises(ValueError):
        SourceTaxAssessment(facts=extract_source_tax_facts(synthetic_ubl()), issue=SourceDocumentIssue.DOCUMENT_MISSING)


# -------------------------------------------------------------------------------- reasons


@pytest.mark.parametrize(
    "spec",
    [
        DocumentSpec(invoice_withholding=True),
        DocumentSpec(lines=(LineSpec(withholding=True),)),
        DocumentSpec(invoice_type="TEVKIFAT"),
        DocumentSpec(invoice_type="YTBTEVKIFAT"),
    ],
    ids=["invoice-level", "line-level", "TEVKIFAT", "YTBTEVKIFAT"],
)
def test_withholding_documents_force_the_withholding_reason(spec: DocumentSpec) -> None:
    reasons = source_tax_review_reasons(_assess(synthetic_ubl(spec)))

    assert tuple(reason.code for reason in reasons) == (WITHHOLDING,)
    assert reasons[0].source == SOURCE_TAX_REASON_SOURCE


def test_withholding_reason_details_name_the_evidence() -> None:
    reason = source_tax_review_reasons(_assess(synthetic_ubl(invoice_type="TEVKIFAT", invoice_withholding=True)))[0]

    assert dict(reason.details) == {
        "invoice_type_code": "TEVKIFAT",
        "invoice_withholding_tax_totals": "1",
        "line_withholding_tax_totals": "0",
    }


def test_unsupported_tax_code_forces_unsupported_content_reason() -> None:
    lines = (LineSpec(taxes=(TaxSpec(code="4080", name="ÖİV"),)),)
    reasons = source_tax_review_reasons(_assess(synthetic_ubl(lines=lines)))

    assert tuple(reason.code for reason in reasons) == (UNSUPPORTED,)
    assert reasons[0].source == SOURCE_TAX_REASON_SOURCE
    assert ("unsupported_tax_type_codes", "4080") in reasons[0].details


def test_withholding_and_unsupported_code_report_both() -> None:
    spec = DocumentSpec(invoice_type="TEVKIFAT", lines=(LineSpec(taxes=(TaxSpec(), TaxSpec(code="4080"))),))

    assert _codes(_assess(synthetic_ubl(spec))) == (WITHHOLDING, UNSUPPORTED)


@pytest.mark.parametrize("issue", list(SourceDocumentIssue))
def test_every_unverified_issue_forces_only_the_unverified_reason(issue: SourceDocumentIssue) -> None:
    reasons = source_tax_review_reasons(SourceTaxAssessment.unverified(issue))

    assert tuple(reason.code for reason in reasons) == (UNVERIFIED,)
    assert reasons[0].source == SOURCE_TAX_REASON_SOURCE
    assert reasons[0].details == (("issue", issue.value),)
