from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.billing.dto import (
    CustomerInvoice,
    CustomerInvoiceBillingInstruction,
    CustomerInvoiceLine,
    VendorBill,
    VendorBillLine,
)
from app.billing.exceptions import CustomerInvoiceBuildError, VendorBillBuildError
from app.billing.validation import VendorBillValidationResult, validation_result
from app.domain.invoice import InternalInvoice, InvoiceLine
from app.matching import InvoiceProductMatchResult, PartnerMatchResult, PartnerMatchStatus, ProductMatchStatus
from app.tax_mapping import InvoiceTaxMappingResult, TaxMatchStatus


class VendorBillBuilder:
    def build(
        self,
        invoice: InternalInvoice,
        partner_match: PartnerMatchResult,
        product_match: InvoiceProductMatchResult,
        tax_match: InvoiceTaxMappingResult,
    ) -> VendorBill:
        validation = validate_vendor_bill_inputs(invoice, partner_match, product_match, tax_match)
        if not validation.is_valid:
            raise VendorBillBuildError(validation.errors)

        assert partner_match.partner_id is not None
        product_by_line = _product_results_by_line(product_match)
        tax_ids_by_line = _tax_ids_by_line(tax_match)
        return VendorBill(
            supplier_id=partner_match.partner_id,
            invoice_number=invoice.header.invoice_number.strip(),
            invoice_date=invoice.header.issue_date,
            currency=invoice.header.currency_code.strip(),
            external_uuid=invoice.header.invoice_uuid.strip() or invoice.header.ettn,
            reference=invoice.header.invoice_number.strip(),
            invoice_lines=tuple(
                _vendor_bill_line(line, product_by_line[line.line_number], tax_ids_by_line) for line in invoice.lines
            ),
            notes=tuple(note.strip() for note in invoice.header.notes if note and note.strip()),
        )


class CustomerInvoiceBuilder:
    def build(
        self,
        *,
        company_id: int,
        source_invoice_id: str,
        invoice: InternalInvoice,
        billing_instruction: CustomerInvoiceBillingInstruction,
    ) -> CustomerInvoice:
        validation = validate_customer_invoice_inputs(
            company_id=company_id,
            source_invoice_id=source_invoice_id,
            invoice=invoice,
            billing_instruction=billing_instruction,
        )
        if not validation.is_valid:
            raise CustomerInvoiceBuildError(validation.errors)

        assert invoice.header.issue_date is not None
        invoice_lines = tuple(
            CustomerInvoiceLine(
                product_id=line.product_id,
                quantity=line.quantity,
                unit_price=line.unit_price,
                tax_ids=line.sales_tax_ids,
                description=line.description,
                source_allocation_key=line.allocation_key,
            )
            for line in billing_instruction.lines
        )
        return CustomerInvoice(
            company_id=company_id,
            customer_id=billing_instruction.customer_id,
            invoice_date=invoice.header.issue_date,
            currency=billing_instruction.currency,
            external_uuid=invoice.header.invoice_uuid.strip() or invoice.header.ettn,
            reference=_customer_invoice_reference(
                source_invoice_id=source_invoice_id,
                billing_instruction=billing_instruction,
            ),
            invoice_lines=invoice_lines,
            notes=(f"Source invoice: {source_invoice_id}",),
        )


def validate_customer_invoice_inputs(
    *,
    company_id: object,
    source_invoice_id: object,
    invoice: object,
    billing_instruction: object,
) -> VendorBillValidationResult:
    errors: list[str] = []
    if type(company_id) is not int or company_id <= 0:
        errors.append("company_id must be a positive integer.")
    if not isinstance(source_invoice_id, str) or not source_invoice_id.strip():
        errors.append("source_invoice_id is required.")
    if not isinstance(invoice, InternalInvoice):
        return validation_result(errors + ["InternalInvoice DTO is required."])
    if not isinstance(billing_instruction, CustomerInvoiceBillingInstruction):
        return validation_result(errors + ["Customer Invoice billing instruction is required."])
    if invoice.header.issue_date is None:
        errors.append("Invoice date is required.")
    return validation_result(errors)


def validate_vendor_bill_inputs(
    invoice: object,
    partner_match: object,
    product_match: object,
    tax_match: object,
) -> VendorBillValidationResult:
    errors: list[str] = []
    if not isinstance(invoice, InternalInvoice):
        return validation_result(["InternalInvoice DTO is required."])
    if not isinstance(partner_match, PartnerMatchResult):
        return validation_result(["PartnerMatchResult DTO is required."])
    if not isinstance(product_match, InvoiceProductMatchResult):
        return validation_result(["InvoiceProductMatchResult DTO is required."])
    if not isinstance(tax_match, InvoiceTaxMappingResult):
        return validation_result(["InvoiceTaxMappingResult DTO is required."])

    if partner_match.status is not PartnerMatchStatus.MATCHED or partner_match.partner_id is None:
        errors.append("Supplier partner must be matched before building a vendor bill.")
    if not invoice.header.invoice_number.strip():
        errors.append("Invoice number is required.")
    if invoice.header.issue_date is None:
        errors.append("Invoice date is required.")
    if invoice.header.currency_code is None or not invoice.header.currency_code.strip():
        errors.append("Invoice currency is required.")
    if not invoice.lines:
        errors.append("At least one invoice line is required.")

    product_by_line, product_errors = _validated_product_results(product_match)
    errors.extend(product_errors)
    tax_by_line, tax_errors = _validated_tax_results(tax_match)
    errors.extend(tax_errors)

    for index, line in enumerate(invoice.lines):
        line_path = f"lines[{index}]"
        if line.line_number is None or not line.line_number.strip():
            errors.append(f"{line_path}.line_number is required.")
            continue
        if line.line_number not in product_by_line:
            errors.append(f"{line_path}.product must be matched.")
        if line.quantity is None or line.quantity <= Decimal("0"):
            errors.append(f"{line_path}.quantity must be greater than zero.")
        if line.unit_price is None:
            errors.append(f"{line_path}.unit_price is required.")
        elif line.unit_price < Decimal("0"):
            errors.append(f"{line_path}.unit_price must not be negative.")
        for tax_index, _tax in enumerate(line.taxes):
            if (line.line_number, tax_index) not in tax_by_line:
                errors.append(f"{line_path}.taxes[{tax_index}] must be matched.")

    return validation_result(errors)


def to_odoo_account_move_payload(vendor_bill: VendorBill, *, currency_id: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "move_type": "in_invoice",
        "partner_id": vendor_bill.supplier_id,
        "invoice_date": vendor_bill.invoice_date.isoformat(),
        "ref": vendor_bill.reference,
        "currency": vendor_bill.currency,
        "invoice_line_ids": tuple((0, 0, _line_payload(line)) for line in vendor_bill.invoice_lines),
    }
    if currency_id is not None:
        payload["currency_id"] = currency_id
    if vendor_bill.notes:
        payload["narration"] = "\n".join(vendor_bill.notes)
    return {key: value for key, value in payload.items() if value is not None}


def to_odoo_customer_invoice_payload(
    customer_invoice: CustomerInvoice,
    *,
    currency_id: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "move_type": "out_invoice",
        "company_id": customer_invoice.company_id,
        "partner_id": customer_invoice.customer_id,
        "invoice_date": customer_invoice.invoice_date.isoformat(),
        "ref": customer_invoice.reference,
        "currency": customer_invoice.currency,
        "invoice_line_ids": tuple((0, 0, _customer_line_payload(line)) for line in customer_invoice.invoice_lines),
    }
    if currency_id is not None:
        payload["currency_id"] = currency_id
    if customer_invoice.notes:
        payload["narration"] = "\n".join(customer_invoice.notes)
    return {key: value for key, value in payload.items() if value is not None}


def _line_payload(line: VendorBillLine) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "product_id": line.product_id,
        "quantity": _decimal_text(line.quantity),
        "price_unit": _decimal_text(line.unit_price),
        "tax_ids": ((6, 0, line.tax_ids),),
        "name": line.description,
    }
    if line.uom is not None:
        payload["product_uom_id"] = line.uom
    return {key: value for key, value in payload.items() if value is not None}


def _customer_line_payload(line: CustomerInvoiceLine) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "product_id": line.product_id,
            "quantity": _decimal_text(line.quantity),
            "price_unit": _decimal_text(line.unit_price),
            "tax_ids": ((6, 0, line.tax_ids),),
            "name": line.description,
        }.items()
        if value is not None
    }


def _vendor_bill_line(
    line: InvoiceLine,
    product_result: Any,
    tax_ids_by_line: dict[tuple[str | None, int], int],
) -> VendorBillLine:
    assert product_result.product_id is not None
    assert line.quantity is not None
    assert line.unit_price is not None
    tax_ids = tuple(tax_ids_by_line[(line.line_number, tax_index)] for tax_index, _tax in enumerate(line.taxes))
    return VendorBillLine(
        product_id=product_result.product_id,
        quantity=line.quantity,
        uom=line.unit_code,
        unit_price=line.unit_price,
        tax_ids=tax_ids,
        description=line.description,
    )


def _customer_invoice_reference(
    *,
    source_invoice_id: str,
    billing_instruction: CustomerInvoiceBillingInstruction,
) -> str:
    return f"Recharge {source_invoice_id}:{billing_instruction.billing_key}"


def _validated_product_results(
    product_match: InvoiceProductMatchResult,
) -> tuple[dict[str | None, Any], tuple[str, ...]]:
    errors = list(product_match.errors)
    product_by_line: dict[str | None, Any] = {}
    for line_result in product_match.line_results:
        line_number = line_result.line_number
        if line_number in product_by_line:
            errors.append(f"Duplicate product mapping for line {line_number}.")
            continue
        result = line_result.result
        if result.status is not ProductMatchStatus.MATCHED or result.product_id is None:
            errors.append(f"Product mapping for line {line_number} is not matched.")
            continue
        product_by_line[line_number] = result
    return product_by_line, tuple(errors)


def _validated_tax_results(
    tax_match: InvoiceTaxMappingResult,
) -> tuple[dict[tuple[str | None, int], Any], tuple[str, ...]]:
    errors = list(tax_match.errors)
    tax_by_line: dict[tuple[str | None, int], Any] = {}
    for line_result in tax_match.line_results:
        key = (line_result.line_number, line_result.tax_index)
        if key in tax_by_line:
            errors.append(f"Duplicate tax mapping for line {line_result.line_number} tax {line_result.tax_index}.")
            continue
        result = line_result.result
        if result.status is not TaxMatchStatus.MATCHED or result.tax_id is None:
            errors.append(f"Tax mapping for line {line_result.line_number} tax {line_result.tax_index} is not matched.")
            continue
        tax_by_line[key] = result
    return tax_by_line, tuple(errors)


def _product_results_by_line(product_match: InvoiceProductMatchResult) -> dict[str | None, Any]:
    return {line_result.line_number: line_result.result for line_result in product_match.line_results}


def _tax_ids_by_line(tax_match: InvoiceTaxMappingResult) -> dict[tuple[str | None, int], int]:
    return {
        (line_result.line_number, line_result.tax_index): line_result.result.tax_id
        for line_result in tax_match.line_results
        if line_result.result.tax_id is not None
    }


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")
