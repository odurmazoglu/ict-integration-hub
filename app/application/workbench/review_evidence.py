from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.application.workbench.evidence import ReviewExecutionEvidence
from app.application.workbench.exceptions import ReviewNotFoundError
from app.domain.invoice import InvoiceLine
from app.erp.models import Partner
from app.matching import ProductMatchResult


@dataclass(frozen=True, slots=True)
class SupplierCandidate:
    partner_id: int
    name: str | None
    vat: str | None
    active: bool
    company_type: str | None
    parent_id: int | None
    commercial_partner_id: int | None
    street: str | None
    street2: str | None
    zip_code: str | None
    city: str | None
    state_id: int | None
    country_id: int | None
    email: str | None
    phone: str | None
    mobile: str | None
    website: str | None
    supplier_rank: int | None
    customer_rank: int | None
    company_id: int | None

    @classmethod
    def from_partner(cls, partner: Partner) -> SupplierCandidate:
        return cls(
            partner_id=partner.id,
            name=partner.name,
            vat=partner.tax_number,
            active=partner.active,
            company_type=partner.company_type,
            parent_id=partner.parent_id,
            commercial_partner_id=partner.commercial_partner_id,
            street=partner.street,
            street2=partner.street2,
            zip_code=partner.zip_code,
            city=partner.city,
            state_id=partner.state_id,
            country_id=partner.country_id,
            email=partner.email,
            phone=partner.phone,
            mobile=partner.mobile,
            website=partner.website,
            supplier_rank=partner.supplier_rank,
            customer_rank=partner.customer_rank,
            company_id=partner.company_id,
        )


@dataclass(frozen=True, slots=True)
class SourceLineEvidence:
    line_number: str | None
    description: str | None
    quantity: Decimal | None
    unit_code: str | None
    unit_price: Decimal | None
    gross_amount: Decimal | None
    discount_amount: Decimal | None
    net_amount: Decimal | None
    taxes: tuple[tuple[str | None, Decimal | None, Decimal | None], ...]
    seller_item_code: str | None
    buyer_item_code: str | None
    product_match: ProductMatchResult | None

    @classmethod
    def from_line(cls, line: InvoiceLine, product_match: ProductMatchResult | None) -> SourceLineEvidence:
        discount_amount = (
            sum((discount.amount or Decimal("0")) for discount in line.discounts) if line.discounts else None
        )
        return cls(
            line_number=line.line_number,
            description=line.description,
            quantity=line.quantity,
            unit_code=line.unit_code,
            unit_price=line.unit_price,
            gross_amount=(line.line_extension_amount + discount_amount if discount_amount is not None else None),
            discount_amount=discount_amount,
            net_amount=line.line_extension_amount,
            taxes=tuple((tax.tax_type, tax.rate, tax.tax_amount) for tax in line.taxes),
            seller_item_code=line.seller_item_code,
            buyer_item_code=line.buyer_item_code,
            product_match=product_match,
        )


@dataclass(frozen=True, slots=True)
class ReviewEvidence:
    supplier_candidates: tuple[SupplierCandidate, ...] = ()
    source_lines: tuple[SourceLineEvidence, ...] = ()


class ReviewEvidenceReader:
    def __init__(self, *, source_reader, execution_reader, partner_repository) -> None:
        self._source_reader = source_reader
        self._execution_reader = execution_reader
        self._partner_repository = partner_repository

    def get(self, *, review_id: str, company_id: int, review_version: int) -> ReviewEvidence | None:
        try:
            source = self._source_reader.get(review_id=review_id, company_id=company_id)
        except ReviewNotFoundError:
            return None
        execution = self._execution_evidence(review_id=review_id, company_id=company_id, review_version=review_version)
        product_by_line = (
            {result.line_number: result.result for result in execution.product_match.line_results}
            if execution is not None
            else {}
        )
        source_lines = tuple(
            SourceLineEvidence.from_line(line, product_by_line.get(line.line_number)) for line in source.invoice.lines
        )
        tax_number = source.invoice.supplier.tax_number
        candidates = (
            ()
            if not tax_number
            else tuple(
                SupplierCandidate.from_partner(partner)
                for partner in self._partner_repository.find_by_tax_number(tax_number, company_id=company_id)
                if partner.active
            )
        )
        return ReviewEvidence(supplier_candidates=candidates, source_lines=source_lines)

    def _execution_evidence(
        self, *, review_id: str, company_id: int, review_version: int
    ) -> ReviewExecutionEvidence | None:
        try:
            return self._execution_reader.get_review_execution_evidence(
                review_id=review_id, company_id=company_id, review_version=review_version
            )
        except ReviewNotFoundError:
            return None
