from __future__ import annotations

from datetime import date, time
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.execution.contracts import ExecutionSourceInvoice
from app.application.execution.exceptions import (
    ExecutionSourceInvoiceError,
    ExecutionSourceInvoiceIntegrityError,
    ExecutionSourceInvoiceNotFoundError,
)
from app.domain.invoice import (
    Address,
    Attachment,
    Discount,
    Header,
    InternalInvoice,
    InvoiceLine,
    MonetaryTotals,
    Party,
    Tax,
)
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.models.execution_source_invoice_evidence import ExecutionSourceInvoiceEvidence
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_item import WorkbenchReviewItem
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType

SAFE_SOURCE_ERROR = "Execution source invoice evidence could not be loaded safely."
SAFE_SOURCE_NOT_FOUND = "Execution source invoice evidence was not found."
SAFE_SOURCE_INTEGRITY_ERROR = "Execution source invoice evidence is invalid."


class SqlAlchemyExecutionSourceInvoiceReader:
    """Load version-pinned execution source evidence from Hub persistence."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_source_invoice(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
    ) -> ExecutionSourceInvoice:
        _validate_query(review_id=review_id, company_id=company_id, decision_version=decision_version)
        try:
            decision = self._accepted_decision(
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
            )
            review_item = self._review_item(review_id=review_id, company_id=company_id)
            evidence = self._evidence_for_decision(decision)
            source = _source_from_evidence(evidence)
            _validate_source_linkage(source=source, decision=decision, review_item=review_item, evidence=evidence)
            return source
        except ExecutionSourceInvoiceError:
            raise
        except SQLAlchemyError as exc:
            raise ExecutionSourceInvoiceError(SAFE_SOURCE_ERROR) from exc
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR) from exc

    def _accepted_decision(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
    ) -> WorkbenchReviewDecision:
        records = tuple(
            self._session.scalars(
                select(WorkbenchReviewDecision)
                .where(
                    WorkbenchReviewDecision.review_id == review_id,
                    WorkbenchReviewDecision.company_id == company_id,
                    WorkbenchReviewDecision.review_version_after == decision_version,
                )
                .order_by(WorkbenchReviewDecision.id.asc())
                .limit(2)
            )
        )
        if not records:
            raise ExecutionSourceInvoiceNotFoundError(SAFE_SOURCE_NOT_FOUND)
        if len(records) > 1:
            raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
        return records[0]

    def _review_item(self, *, review_id: str, company_id: int) -> WorkbenchReviewItem:
        records = tuple(
            self._session.scalars(
                select(WorkbenchReviewItem)
                .where(
                    WorkbenchReviewItem.review_id == review_id,
                    WorkbenchReviewItem.company_id == company_id,
                )
                .order_by(WorkbenchReviewItem.id.asc())
                .limit(2)
            )
        )
        if not records:
            raise ExecutionSourceInvoiceNotFoundError(SAFE_SOURCE_NOT_FOUND)
        if len(records) > 1:
            raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
        return records[0]

    def _evidence_for_decision(self, decision: WorkbenchReviewDecision) -> ExecutionSourceInvoiceEvidence:
        records = tuple(
            self._session.scalars(
                select(ExecutionSourceInvoiceEvidence)
                .where(
                    ExecutionSourceInvoiceEvidence.review_id == decision.review_id,
                    ExecutionSourceInvoiceEvidence.company_id == decision.company_id,
                    ExecutionSourceInvoiceEvidence.decision_version == decision.review_version_after,
                    ExecutionSourceInvoiceEvidence.decision_id == decision.decision_id,
                )
                .order_by(ExecutionSourceInvoiceEvidence.id.asc())
                .limit(2)
            )
        )
        if not records:
            raise ExecutionSourceInvoiceNotFoundError(SAFE_SOURCE_NOT_FOUND)
        if len(records) > 1:
            raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
        return records[0]


def serialize_execution_source_invoice(source: ExecutionSourceInvoice, *, decision_id: str) -> dict[str, Any]:
    """Serialize immutable source evidence for persistence by future import/review writers."""

    _require_text(decision_id)
    return serialize_execution_source_invoice_payload(source) | {
        "decision_id": decision_id,
    }


def serialize_execution_source_invoice_payload(source: ExecutionSourceInvoice) -> dict[str, Any]:
    """Serialize immutable invoice/match evidence without coupling to accepted decisions."""

    return {
        "review_id": source.review_id,
        "company_id": source.company_id,
        "decision_version": source.decision_version,
        "source_invoice_id": source.source_invoice_id,
        "invoice": _invoice_to_data(source.invoice),
        "partner_match": _partner_match_to_data(source.partner_match),
        "product_match": _product_match_to_data(source.product_match),
        "tax_match": _tax_match_to_data(source.tax_match),
    }


def deserialize_execution_source_invoice_payload(data: dict[str, Any]) -> ExecutionSourceInvoice:
    """Hydrate immutable invoice/match evidence from the canonical JSON shape."""

    source = ExecutionSourceInvoice(
        review_id=_required_text(data.get("review_id")),
        company_id=_required_int(data.get("company_id")),
        decision_version=_required_int(data.get("decision_version")),
        source_invoice_id=_required_text(data.get("source_invoice_id")),
        invoice=_invoice_from_data(_require_dict(data.get("invoice"))),
        partner_match=_partner_match_from_data(_require_dict(data.get("partner_match"))),
        product_match=_product_match_from_data(_require_dict(data.get("product_match"))),
        tax_match=_tax_match_from_data(_require_dict(data.get("tax_match"))),
    )
    invoice_identity = source.invoice.header.ettn or source.invoice.header.invoice_uuid
    if source.source_invoice_id != invoice_identity:
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    return source


def _validate_query(*, review_id: str, company_id: int, decision_version: int) -> None:
    _require_text(review_id)
    if type(company_id) is not int or company_id <= 0:
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    if type(decision_version) is not int or decision_version <= 0:
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)


def _source_from_evidence(evidence: ExecutionSourceInvoiceEvidence) -> ExecutionSourceInvoice:
    return deserialize_execution_source_invoice_payload(
        {
            "review_id": evidence.review_id,
            "company_id": evidence.company_id,
            "decision_version": evidence.decision_version,
            "source_invoice_id": evidence.source_invoice_id,
            "invoice": evidence.invoice,
            "partner_match": evidence.partner_match,
            "product_match": evidence.product_match,
            "tax_match": evidence.tax_match,
        }
    )


def _validate_source_linkage(
    *,
    source: ExecutionSourceInvoice,
    decision: WorkbenchReviewDecision,
    review_item: WorkbenchReviewItem,
    evidence: ExecutionSourceInvoiceEvidence,
) -> None:
    if source.review_id != decision.review_id or evidence.review_id != decision.review_id:
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    if source.company_id != decision.company_id or evidence.company_id != decision.company_id:
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    if review_item.review_id != decision.review_id or review_item.company_id != decision.company_id:
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    if source.decision_version != decision.review_version_after:
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    invoice_identity = source.invoice.header.ettn or source.invoice.header.invoice_uuid
    if source.source_invoice_id != invoice_identity:
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    if review_item.invoice_id != source.source_invoice_id:
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)


def _invoice_to_data(invoice: InternalInvoice) -> dict[str, Any]:
    return {
        "header": {
            "invoice_number": invoice.header.invoice_number,
            "invoice_uuid": invoice.header.invoice_uuid,
            "ettn": invoice.header.ettn,
            "invoice_type": invoice.header.invoice_type,
            "profile_id": invoice.header.profile_id,
            "issue_date": _date_to_data(invoice.header.issue_date),
            "issue_time": _time_to_data(invoice.header.issue_time),
            "currency_code": invoice.header.currency_code,
            "exchange_rate": _decimal_to_data(invoice.header.exchange_rate),
            "notes": list(invoice.header.notes),
        },
        "supplier": _party_to_data(invoice.supplier),
        "customer": _party_to_data(invoice.customer),
        "totals": {
            "line_extension_amount": _decimal_to_data(invoice.totals.line_extension_amount),
            "tax_exclusive_amount": _decimal_to_data(invoice.totals.tax_exclusive_amount),
            "tax_inclusive_amount": _decimal_to_data(invoice.totals.tax_inclusive_amount),
            "allowance_total": _decimal_to_data(invoice.totals.allowance_total),
            "charge_total": _decimal_to_data(invoice.totals.charge_total),
            "payable_amount": _decimal_to_data(invoice.totals.payable_amount),
            "rounding_amount": _decimal_to_data(invoice.totals.rounding_amount),
        },
        "lines": [_invoice_line_to_data(line) for line in invoice.lines],
        "attachments": [
            {
                "filename": attachment.filename,
                "mime_type": attachment.mime_type,
                "sha256": attachment.sha256,
                "size": attachment.size,
            }
            for attachment in invoice.attachments
        ],
    }


def _invoice_from_data(data: dict[str, Any]) -> InternalInvoice:
    header = _require_dict(data.get("header"))
    totals = _require_dict(data.get("totals"))
    return InternalInvoice(
        header=Header(
            invoice_number=_required_text(header.get("invoice_number")),
            invoice_uuid=_required_text(header.get("invoice_uuid")),
            ettn=_optional_text(header.get("ettn")),
            invoice_type=_optional_text(header.get("invoice_type")),
            profile_id=_optional_text(header.get("profile_id")),
            issue_date=_optional_date(header.get("issue_date")),
            issue_time=_optional_time(header.get("issue_time")),
            currency_code=_optional_text(header.get("currency_code")),
            exchange_rate=_optional_decimal(header.get("exchange_rate")),
            notes=tuple(_text_list(header.get("notes", ()))),
        ),
        supplier=_party_from_data(_require_dict(data.get("supplier"))),
        customer=_party_from_data(_require_dict(data.get("customer"))),
        totals=MonetaryTotals(
            line_extension_amount=_optional_decimal(totals.get("line_extension_amount")),
            tax_exclusive_amount=_optional_decimal(totals.get("tax_exclusive_amount")),
            tax_inclusive_amount=_optional_decimal(totals.get("tax_inclusive_amount")),
            allowance_total=_optional_decimal(totals.get("allowance_total")),
            charge_total=_optional_decimal(totals.get("charge_total")),
            payable_amount=_optional_decimal(totals.get("payable_amount")),
            rounding_amount=_optional_decimal(totals.get("rounding_amount")),
        ),
        lines=tuple(_invoice_line_from_data(_require_dict(line)) for line in _list(data.get("lines", ()))),
        attachments=tuple(
            _attachment_from_data(_require_dict(attachment)) for attachment in _list(data.get("attachments", ()))
        ),
    )


def _party_to_data(party: Party) -> dict[str, Any]:
    return {
        "name": party.name,
        "tax_number": party.tax_number,
        "tax_office": party.tax_office,
        "mersis_number": party.mersis_number,
        "website": party.website,
        "emails": list(party.emails),
        "phones": list(party.phones),
        "addresses": [
            {
                "street": address.street,
                "building_number": address.building_number,
                "city": address.city,
                "district": address.district,
                "postal_code": address.postal_code,
                "country": address.country,
            }
            for address in party.addresses
        ],
    }


def _party_from_data(data: dict[str, Any]) -> Party:
    return Party(
        name=_optional_text(data.get("name")),
        tax_number=_optional_text(data.get("tax_number")),
        tax_office=_optional_text(data.get("tax_office")),
        mersis_number=_optional_text(data.get("mersis_number")),
        website=_optional_text(data.get("website")),
        emails=tuple(_text_list(data.get("emails", ()))),
        phones=tuple(_text_list(data.get("phones", ()))),
        addresses=tuple(_address_from_data(_require_dict(address)) for address in _list(data.get("addresses", ()))),
    )


def _address_from_data(data: dict[str, Any]) -> Address:
    return Address(
        street=_optional_text(data.get("street")),
        building_number=_optional_text(data.get("building_number")),
        city=_optional_text(data.get("city")),
        district=_optional_text(data.get("district")),
        postal_code=_optional_text(data.get("postal_code")),
        country=_optional_text(data.get("country")),
    )


def _invoice_line_to_data(line: InvoiceLine) -> dict[str, Any]:
    return {
        "line_number": line.line_number,
        "description": line.description,
        "seller_item_code": line.seller_item_code,
        "buyer_item_code": line.buyer_item_code,
        "barcode": line.barcode,
        "quantity": _decimal_to_data(line.quantity),
        "unit_code": line.unit_code,
        "unit_price": _decimal_to_data(line.unit_price),
        "line_extension_amount": _decimal_to_data(line.line_extension_amount),
        "discounts": [
            {
                "amount": _decimal_to_data(discount.amount),
                "reason": discount.reason,
                "rate": _decimal_to_data(discount.rate),
            }
            for discount in line.discounts
        ],
        "taxes": [_tax_to_data(tax) for tax in line.taxes],
    }


def _invoice_line_from_data(data: dict[str, Any]) -> InvoiceLine:
    return InvoiceLine(
        line_number=_optional_text(data.get("line_number")),
        description=_optional_text(data.get("description")),
        seller_item_code=_optional_text(data.get("seller_item_code")),
        buyer_item_code=_optional_text(data.get("buyer_item_code")),
        barcode=_optional_text(data.get("barcode")),
        quantity=_optional_decimal(data.get("quantity")),
        unit_code=_optional_text(data.get("unit_code")),
        unit_price=_optional_decimal(data.get("unit_price")),
        line_extension_amount=_optional_decimal(data.get("line_extension_amount")),
        discounts=tuple(_discount_from_data(_require_dict(discount)) for discount in _list(data.get("discounts", ()))),
        taxes=tuple(_tax_from_data(_require_dict(tax)) for tax in _list(data.get("taxes", ()))),
    )


def _tax_to_data(tax: Tax) -> dict[str, Any]:
    return {
        "tax_type": tax.tax_type,
        "rate": _decimal_to_data(tax.rate),
        "base_amount": _decimal_to_data(tax.base_amount),
        "tax_amount": _decimal_to_data(tax.tax_amount),
        "exemption_reason": tax.exemption_reason,
    }


def _tax_from_data(data: dict[str, Any]) -> Tax:
    return Tax(
        tax_type=_optional_text(data.get("tax_type")),
        rate=_optional_decimal(data.get("rate")),
        base_amount=_optional_decimal(data.get("base_amount")),
        tax_amount=_optional_decimal(data.get("tax_amount")),
        exemption_reason=_optional_text(data.get("exemption_reason")),
    )


def _discount_from_data(data: dict[str, Any]) -> Discount:
    return Discount(
        amount=_optional_decimal(data.get("amount")),
        reason=_optional_text(data.get("reason")),
        rate=_optional_decimal(data.get("rate")),
    )


def _attachment_from_data(data: dict[str, Any]) -> Attachment:
    return Attachment(
        filename=_optional_text(data.get("filename")),
        mime_type=_optional_text(data.get("mime_type")),
        sha256=_optional_text(data.get("sha256")),
        size=_optional_int(data.get("size")),
    )


def _partner_match_to_data(result: PartnerMatchResult) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "partner_id": result.partner_id,
        "matched_by": result.matched_by,
        "reason": result.reason,
        "candidate_count": result.candidate_count,
        "confidence": _decimal_to_data(result.confidence),
    }


def _partner_match_from_data(data: dict[str, Any]) -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus(str(data["status"])),
        partner_id=_optional_int(data.get("partner_id")),
        matched_by=_optional_text(data.get("matched_by")),
        reason=_required_text(data.get("reason")),
        candidate_count=_required_int(data.get("candidate_count")),
        confidence=_optional_decimal(data.get("confidence")),
    )


def _product_match_to_data(result: InvoiceProductMatchResult) -> dict[str, Any]:
    return {
        "line_results": [
            {
                "line_number": line_result.line_number,
                "result": _product_line_match_to_data(line_result.result),
            }
            for line_result in result.line_results
        ],
        "warnings": list(result.warnings),
        "errors": list(result.errors),
    }


def _product_match_from_data(data: dict[str, Any]) -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(
        line_results=tuple(
            InvoiceProductLineResult(
                line_number=_optional_text(_require_dict(line).get("line_number")),
                result=_product_line_match_from_data(_require_dict(_require_dict(line).get("result"))),
            )
            for line in _list(data["line_results"])
        ),
        warnings=tuple(_text_list(data.get("warnings", ()))),
        errors=tuple(_text_list(data.get("errors", ()))),
    )


def _product_line_match_to_data(result: ProductMatchResult) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "line_number": result.line_number,
        "product_id": result.product_id,
        "default_code": result.default_code,
        "barcode": result.barcode,
        "seller_item_code": result.seller_item_code,
        "matched_by": result.matched_by,
        "reason": result.reason,
        "candidate_count": result.candidate_count,
        "confidence": _decimal_to_data(result.confidence),
    }


def _product_line_match_from_data(data: dict[str, Any]) -> ProductMatchResult:
    return ProductMatchResult(
        status=ProductMatchStatus(str(data["status"])),
        line_number=_optional_text(data.get("line_number")),
        product_id=_optional_int(data.get("product_id")),
        default_code=_optional_text(data.get("default_code")),
        barcode=_optional_text(data.get("barcode")),
        seller_item_code=_optional_text(data.get("seller_item_code")),
        matched_by=_optional_text(data.get("matched_by")),
        reason=_required_text(data.get("reason")),
        candidate_count=_required_int(data.get("candidate_count")),
        confidence=_optional_decimal(data.get("confidence")),
    )


def _tax_match_to_data(result: InvoiceTaxMappingResult) -> dict[str, Any]:
    return {
        "line_results": [
            {
                "line_number": line_result.line_number,
                "tax_index": line_result.tax_index,
                "result": _tax_line_match_to_data(line_result.result),
            }
            for line_result in result.line_results
        ],
        "warnings": list(result.warnings),
        "errors": list(result.errors),
    }


def _tax_match_from_data(data: dict[str, Any]) -> InvoiceTaxMappingResult:
    return InvoiceTaxMappingResult(
        line_results=tuple(
            InvoiceTaxLineResult(
                line_number=_optional_text(_require_dict(line).get("line_number")),
                tax_index=_required_int(_require_dict(line).get("tax_index")),
                result=_tax_line_match_from_data(_require_dict(_require_dict(line).get("result"))),
            )
            for line in _list(data["line_results"])
        ),
        warnings=tuple(_text_list(data.get("warnings", ()))),
        errors=tuple(_text_list(data.get("errors", ()))),
    )


def _tax_line_match_to_data(result: TaxMatchResult) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "tax_id": result.tax_id,
        "company_id": result.company_id,
        "tax_type": result.tax_type.value if result.tax_type is not None else None,
        "tax_rate": _decimal_to_data(result.tax_rate),
        "matched_by": result.matched_by,
        "confidence": _decimal_to_data(result.confidence),
        "reason": result.reason,
        "candidate_count": result.candidate_count,
    }


def _tax_line_match_from_data(data: dict[str, Any]) -> TaxMatchResult:
    return TaxMatchResult(
        status=TaxMatchStatus(str(data["status"])),
        tax_id=_optional_int(data.get("tax_id")),
        company_id=_optional_int(data.get("company_id")),
        tax_type=TaxType(str(data["tax_type"])) if data.get("tax_type") is not None else None,
        tax_rate=_optional_decimal(data.get("tax_rate")),
        matched_by=_optional_text(data.get("matched_by")),
        confidence=_optional_decimal(data.get("confidence")),
        reason=_required_text(data.get("reason")),
        candidate_count=_required_int(data.get("candidate_count")),
    )


def _require_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    return value


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list | tuple):
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    return list(value)


def _text_list(value: Any) -> tuple[str, ...]:
    return tuple(_required_text(item) for item in _list(value))


def _required_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    return value


def _require_text(value: Any) -> None:
    _required_text(value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    return value


def _required_int(value: Any) -> int:
    if type(value) is not int:
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    return value


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return _required_int(value)


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    decimal = Decimal(value)
    if not decimal.is_finite():
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    return decimal


def _decimal_to_data(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _optional_date(value: Any) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    return date.fromisoformat(value)


def _date_to_data(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _optional_time(value: Any) -> time | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    return time.fromisoformat(value)


def _time_to_data(value: time | None) -> str | None:
    return value.isoformat() if value is not None else None
