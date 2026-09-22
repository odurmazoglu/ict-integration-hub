from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.api.routers.workbench import _evidence_response
from app.application.workbench.evidence import ReviewExecutionEvidence, ReviewSourceInvoiceEvidence
from app.application.workbench.review_evidence import ReviewEvidenceReader
from app.domain.invoice import Discount, Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.erp.models import Partner
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType


class FakeSourceReader:
    def __init__(self, evidence: ReviewSourceInvoiceEvidence | None) -> None:
        self.evidence = evidence
        self.calls: list[tuple[str, int]] = []

    def get(self, *, review_id: str, company_id: int) -> ReviewSourceInvoiceEvidence:
        self.calls.append((review_id, company_id))
        if self.evidence is None:
            from app.application.workbench.exceptions import ReviewNotFoundError

            raise ReviewNotFoundError("Review source invoice evidence was not found.")
        return self.evidence


class FakeExecutionReader:
    def __init__(self, evidence: ReviewExecutionEvidence) -> None:
        self.evidence = evidence
        self.calls: list[tuple[str, int, int]] = []

    def get_review_execution_evidence(self, *, review_id: str, company_id: int, review_version: int):
        self.calls.append((review_id, company_id, review_version))
        return self.evidence


class FakePartnerRepository:
    def __init__(self, records: tuple[Partner, ...]) -> None:
        self.records = records
        self.calls: list[tuple[str, int | None]] = []

    def find_by_tax_number(self, tax_number: str, *, company_id: int | None = None):
        self.calls.append((tax_number, company_id))
        return self.records


def test_review_evidence_exposes_authoritative_candidates_and_source_lines_without_rematching() -> None:
    invoice = _invoice()
    source = ReviewSourceInvoiceEvidence(
        review_id="review-1",
        company_id=7,
        review_version=1,
        source_invoice_id="ETTN-1",
        invoice=invoice,
    )
    persisted_match = ProductMatchResult(
        status=ProductMatchStatus.MATCHED,
        line_number="1",
        product_id=701,
        default_code="BUY-1",
        barcode="BAR-1",
        seller_item_code="SELL-1",
        matched_by="default_code",
        reason="Persisted product match.",
        candidate_count=1,
        confidence=Decimal("1.00"),
    )
    execution = ReviewExecutionEvidence(
        review_id="review-1",
        company_id=7,
        review_version=1,
        source_invoice_id="ETTN-1",
        invoice=invoice,
        partner_match=PartnerMatchResult(
            status=PartnerMatchStatus.MULTIPLE_MATCHES,
            partner_id=None,
            matched_by=None,
            reason="Multiple active supplier partner candidates found by tax number.",
            candidate_count=2,
            confidence=None,
        ),
        product_match=InvoiceProductMatchResult(
            line_results=(InvoiceProductLineResult(line_number="1", result=persisted_match),)
        ),
        tax_match=InvoiceTaxMappingResult(
            line_results=(
                InvoiceTaxLineResult(
                    line_number="1",
                    tax_index=0,
                    result=TaxMatchResult(
                        status=TaxMatchStatus.MATCHED,
                        tax_id=801,
                        company_id=7,
                        tax_type=TaxType.VAT,
                        tax_rate=Decimal("20"),
                        matched_by="rate",
                        confidence=Decimal("1.00"),
                        reason="Persisted tax match.",
                        candidate_count=1,
                    ),
                ),
            )
        ),
    )
    partners = FakePartnerRepository(
        (
            Partner(
                id=101,
                name="Child Supplier",
                tax_number="1760390647",
                active=True,
                company_id=7,
                company_type="company",
                parent_id=201,
                commercial_partner_id=201,
                street="Street 1",
                street2="Floor 2",
                zip_code="34000",
                city="Istanbul",
                state_id=301,
                country_id=302,
                email="a@example.test",
                phone="+90 1",
                mobile="+90 2",
                website="https://a.example.test",
                supplier_rank=4,
                customer_rank=1,
            ),
            Partner(
                id=102,
                name="Parent Supplier",
                tax_number="1760390647",
                active=True,
                company_id=None,
                parent_id=None,
                commercial_partner_id=102,
            ),
            Partner(id=103, name="Inactive", tax_number="1760390647", active=False),
        )
    )
    source_reader = FakeSourceReader(source)
    execution_reader = FakeExecutionReader(execution)

    result = ReviewEvidenceReader(
        source_reader=source_reader,
        execution_reader=execution_reader,
        partner_repository=partners,
    ).get(review_id="review-1", company_id=7, review_version=1)

    assert result is not None
    assert [candidate.partner_id for candidate in result.supplier_candidates] == [101, 102]
    assert result.supplier_candidates[0].parent_id == 201
    assert result.supplier_candidates[0].commercial_partner_id == 201
    assert result.supplier_candidates[0].state_id == 301
    assert result.supplier_candidates[0].country_id == 302
    assert partners.calls == [("1760390647", 7)]
    assert execution_reader.calls == [("review-1", 7, 1)]
    line = result.source_lines[0]
    assert line.unit_code == "C62"
    assert line.gross_amount == Decimal("110")
    assert line.discount_amount == Decimal("10")
    assert line.net_amount == Decimal("100")
    assert line.seller_item_code == "SELL-1"
    assert line.buyer_item_code == "BUY-1"
    assert line.product_match is persisted_match
    assert line.product_match.product_id == 701
    assert line.product_match.reason == "Persisted product match."
    response = _evidence_response(result)
    assert response is not None
    payload = response.model_dump()
    assert payload["supplier_candidates"][0]["partner_id"] == 101
    assert payload["supplier_candidates"][0]["zip"] == "34000"
    assert payload["source_lines"][0]["unit_code"] == "C62"
    assert payload["source_lines"][0]["product_match"]["product_id"] == 701


def test_review_evidence_uses_review_and_context_identity_only() -> None:
    source_reader = FakeSourceReader(None)
    partners = FakePartnerRepository(())

    result = ReviewEvidenceReader(
        source_reader=source_reader,
        execution_reader=FakeExecutionReader(_execution(_invoice())),
        partner_repository=partners,
    ).get(review_id="review-unknown", company_id=99, review_version=1)

    assert result is None
    assert source_reader.calls == [("review-unknown", 99)]
    assert partners.calls == []


def _invoice() -> InternalInvoice:
    return InternalInvoice(
        header=Header(invoice_number="INV-1", invoice_uuid="ETTN-1", ettn="ETTN-1", issue_date=date(2026, 8, 2)),
        supplier=Party(name="Supplier", tax_number="1760390647"),
        customer=Party(name="Customer"),
        totals=MonetaryTotals(payable_amount=Decimal("120")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Service",
                seller_item_code="SELL-1",
                buyer_item_code="BUY-1",
                quantity=Decimal("2"),
                unit_code="C62",
                unit_price=Decimal("50"),
                line_extension_amount=Decimal("100"),
                discounts=(Discount(amount=Decimal("10")),),
                taxes=(Tax(tax_type="VAT", rate=Decimal("20"), tax_amount=Decimal("20")),),
            ),
        ),
    )


def _execution(invoice: InternalInvoice) -> ReviewExecutionEvidence:
    return ReviewExecutionEvidence(
        review_id="review-unknown",
        company_id=99,
        review_version=1,
        source_invoice_id="ETTN-1",
        invoice=invoice,
        partner_match=PartnerMatchResult(
            status=PartnerMatchStatus.NOT_FOUND,
            partner_id=None,
            matched_by=None,
            reason="No match",
            candidate_count=0,
            confidence=None,
        ),
        product_match=InvoiceProductMatchResult(
            line_results=(
                InvoiceProductLineResult(
                    line_number="1",
                    result=ProductMatchResult(
                        status=ProductMatchStatus.NOT_FOUND,
                        line_number="1",
                        product_id=None,
                        default_code="BUY-1",
                        barcode=None,
                        seller_item_code="SELL-1",
                        matched_by=None,
                        reason="No product match.",
                        candidate_count=0,
                        confidence=None,
                    ),
                ),
            )
        ),
        tax_match=InvoiceTaxMappingResult(
            line_results=(
                InvoiceTaxLineResult(
                    line_number="1",
                    tax_index=0,
                    result=TaxMatchResult(
                        status=TaxMatchStatus.NOT_FOUND,
                        tax_id=None,
                        company_id=99,
                        tax_type=TaxType.VAT,
                        tax_rate=Decimal("20"),
                        matched_by=None,
                        confidence=None,
                        reason="No tax match.",
                        candidate_count=0,
                    ),
                ),
            )
        ),
    )
