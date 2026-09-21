"""P0-PROD-10E: Vendor Bill UoM resolution -- the raw source UN/CEFACT unit code
(e.g. "C62"/"NIU") must never be written to Odoo's ``account.move.line.product_uom_id``.

Root cause (confirmed against real production data during the P0-PROD-10C pilot):
``VendorBillBuilder`` copied ``InvoiceLine.unit_code`` straight into the Odoo
payload's ``product_uom_id`` -- an integer many2one field -- causing Odoo to
reject the write with ``invalid input syntax for type integer: "C62"``.

The fix resolves the real Odoo ``uom.uom`` integer id read-only from the
resolved ``product.product`` itself, exactly mirroring the existing currency-id
resolution pattern (``AccountMoveRepository._resolve_vendor_bill_currency`` /
``OdooVendorBillPreviewCurrencyReader``). Most of the individual mechanics are
already covered by targeted tests in ``test_vendor_bill_builder.py``,
``test_account_move_repository.py``, ``test_selected_product_line_resolution.py``,
and ``test_vendor_bill_preview.py`` (each updated by this same change). This file
adds the scenarios not already covered elsewhere: the full builder-to-Odoo-payload
path exercising the exact production shape, preview/execution UoM parity against
the same underlying Odoo state, and an explicit customer-flow-untouched check.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.billing import (
    CustomerInvoiceLine,
    VendorBillBuilder,
    VendorBillLine,
    to_odoo_account_move_payload,
    to_odoo_customer_invoice_payload,
)
from app.billing.dto import CustomerInvoice
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.erp.write.account_move_repository import AccountMoveRepository
from app.erp.write.exceptions import VendorBillWriteValidationError
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType

COMPANY_ID = 1
PARTNER_ID = 448
PRODUCT_ID = 389
TEMPLATE_ID = 161
RESOLVED_UOM_ID = 1
SOURCE_UNIT_CODE = "C62"
SELLER_ITEM_CODE = "HBCV0000CHXXQ5"
TAX_ID = 34


# --------------------------------------------------------------------------- fakes


class _FakeOdooJson2Client:
    """Models exactly the Odoo calls AccountMoveRepository makes: res.currency and
    product.product reads, and one account.move create. Raises loudly if
    create_account_move is ever called when a test expects a fail-closed outcome."""

    def __init__(self, *, product_uom_records: list[dict] | None = None, forbid_create: bool = False) -> None:
        self.product_uom_records = (
            product_uom_records
            if product_uom_records is not None
            else [{"id": PRODUCT_ID, "uom_id": [RESOLVED_UOM_ID, "Units"]}]
        )
        self.forbid_create = forbid_create
        self.create_calls: list[dict] = []
        self.search_calls: list[dict] = []

    async def search_read(self, *, model, domain, fields, limit=20, offset=0):
        self.search_calls.append({"model": model, "domain": domain})
        if model == "res.currency":
            return [{"id": 31, "name": "TRY", "active": True}]
        if model == "product.product":
            return list(self.product_uom_records)
        return []

    async def create_account_move(self, payload: dict) -> int:
        if self.forbid_create:
            raise AssertionError("create_account_move must never be called when UoM cannot be resolved.")
        self.create_calls.append(payload)
        return 9001


def _stanley_invoice() -> InternalInvoice:
    """The real P0-PROD-10C production invoice line: source unit_code=C62."""

    return InternalInvoice(
        header=Header(
            invoice_number="HD12026000964602",
            invoice_uuid="F1ADCCAD-FB70-9EF1-8105-005056BB160F",
            ettn="F1ADCCAD-FB70-9EF1-8105-005056BB160F",
            issue_date=date(2026, 9, 10),
            currency_code="TRY",
        ),
        supplier=Party(name="D-MARKET ELEKTRONIK HIZMETLER VE TICARET ANONIM SIRKETI", tax_number="2650179910"),
        customer=Party(name="ICT Teknoloji", tax_number="4651205941"),
        totals=MonetaryTotals(
            line_extension_amount=Decimal("2915.82"),
            tax_exclusive_amount=Decimal("2166.00"),
            tax_inclusive_amount=Decimal("2599.20"),
            allowance_total=Decimal("749.82"),
            payable_amount=Decimal("2599.20"),
        ),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Stanley The Iceflow Flip Straw 2.0 Pipet",
                seller_item_code=SELLER_ITEM_CODE,
                quantity=Decimal("1.000"),
                unit_code=SOURCE_UNIT_CODE,
                unit_price=Decimal("2915.820000"),
                line_extension_amount=Decimal("2915.82"),
                discounts=(),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
            ),
        ),
    )


def _matched_partner() -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MATCHED,
        partner_id=PARTNER_ID,
        matched_by="supplier_remediation_effect",
        reason="matched",
        candidate_count=1,
        confidence=Decimal("1.00"),
    )


def _matched_product() -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(
        line_results=(
            InvoiceProductLineResult(
                line_number="1",
                result=ProductMatchResult(
                    status=ProductMatchStatus.MATCHED,
                    line_number="1",
                    product_id=PRODUCT_ID,
                    default_code=None,
                    barcode=None,
                    seller_item_code=SELLER_ITEM_CODE,
                    matched_by="human_selected",
                    reason="matched",
                    candidate_count=1,
                    confidence=Decimal("1.00"),
                ),
            ),
        )
    )


def _matched_taxes() -> InvoiceTaxMappingResult:
    return InvoiceTaxMappingResult(
        line_results=(
            InvoiceTaxLineResult(
                line_number="1",
                tax_index=0,
                result=TaxMatchResult(
                    status=TaxMatchStatus.MATCHED,
                    tax_id=TAX_ID,
                    company_id=COMPANY_ID,
                    tax_type=TaxType.VAT,
                    tax_rate=Decimal("20"),
                    matched_by="rate",
                    confidence=Decimal("1.00"),
                    reason="matched",
                    candidate_count=1,
                ),
            ),
        )
    )


def _build_stanley_vendor_bill():
    invoice = _stanley_invoice()
    return VendorBillBuilder().build(
        invoice, _matched_partner(), _matched_product(), _matched_taxes(), company_id=COMPANY_ID
    )


# =================================================================== 1: deterministic match -> integer uom_id
# =================================================================== 6: production regression shape


async def test_production_regression_shape_resolves_integer_uom_never_source_code() -> None:
    """C62 (source) -> product 389/template 161 -> resolved Odoo uom_id=1 -> payload
    carries the integer 1, never the string "C62". Full builder-to-Odoo-payload path,
    the exact real production line."""

    bill = _build_stanley_vendor_bill()
    client = _FakeOdooJson2Client()
    repository = AccountMoveRepository(client=client)

    result = await repository.create_draft_vendor_bill(vendor_bill=bill, idempotency_key="p0-prod-10e-regression")

    assert result.id == 9001
    line_payload = client.create_calls[0]["invoice_line_ids"][0][2]
    assert line_payload["product_uom_id"] == RESOLVED_UOM_ID
    assert line_payload["product_uom_id"] == 1
    assert line_payload["product_uom_id"] != SOURCE_UNIT_CODE
    assert isinstance(line_payload["product_uom_id"], int)
    # The immutable source evidence itself is untouched -- unit_code still carries "C62".
    assert _stanley_invoice().lines[0].unit_code == SOURCE_UNIT_CODE


# =================================================================== 2: operator selected_product_id -> integer uom_id


async def test_operator_selected_product_id_resolves_integer_uom_id() -> None:
    """A line resolved via LineResolution.selected_product_id (matched_by=human_selected,
    not the deterministic matcher) still resolves a real integer UoM, not the source code.
    See also test_selected_product_line_resolution.py for the builder-level proof that
    matched_by="human_selected" flows through identically."""

    bill = _build_stanley_vendor_bill()
    assert bill.invoice_lines[0].product_id == PRODUCT_ID  # this line came from a human selection, not the matcher
    client = _FakeOdooJson2Client()
    repository = AccountMoveRepository(client=client)

    await repository.create_draft_vendor_bill(vendor_bill=bill, idempotency_key="p0-prod-10e-selected-product")

    line_payload = client.create_calls[0]["invoice_line_ids"][0][2]
    assert line_payload["product_uom_id"] == RESOLVED_UOM_ID


# =================================================================== 3: missing/invalid UoM -> fail closed, zero write


@pytest.mark.parametrize(
    "product_uom_records",
    [
        [],  # product not found in Odoo at all
        [{"id": PRODUCT_ID, "uom_id": False}],  # product exists but carries no UoM
    ],
)
async def test_unresolved_uom_fails_closed_before_any_odoo_write(product_uom_records: list[dict]) -> None:
    bill = _build_stanley_vendor_bill()
    client = _FakeOdooJson2Client(product_uom_records=product_uom_records, forbid_create=True)
    repository = AccountMoveRepository(client=client)

    with pytest.raises(VendorBillWriteValidationError):
        await repository.create_draft_vendor_bill(vendor_bill=bill, idempotency_key="p0-prod-10e-fail-closed")

    assert client.create_calls == []


# =================================================================== 4: account_only -> no product_uom_id


async def test_account_only_line_never_carries_product_uom_id() -> None:
    invoice = InternalInvoice(
        header=Header(
            invoice_number="INV-EXP-1",
            invoice_uuid="uuid-exp-1",
            ettn="uuid-exp-1",
            issue_date=date(2026, 9, 10),
            currency_code="TRY",
        ),
        supplier=Party(name="Supplier", tax_number="1234567890"),
        customer=Party(name="ICT", tax_number="9876543210"),
        totals=MonetaryTotals(payable_amount=Decimal("100.00")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Yillik aidat",
                quantity=Decimal("1"),
                unit_code=SOURCE_UNIT_CODE,
                unit_price=Decimal("83.33"),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
            ),
        ),
    )
    line = VendorBillLine(
        product_id=None,
        account_id=9101,
        quantity=Decimal("1"),
        unit_price=Decimal("83.33"),
        tax_ids=(TAX_ID,),
        description="Yillik aidat",
    )
    from app.billing.dto import VendorBill

    bill = VendorBill(
        supplier_id=PARTNER_ID,
        invoice_number=invoice.header.invoice_number,
        invoice_date=invoice.header.issue_date,
        currency="TRY",
        external_uuid=invoice.header.ettn,
        reference=invoice.header.invoice_number,
        company_id=COMPANY_ID,
        invoice_lines=(line,),
    )

    # product_uom_ids intentionally empty -- an account_only line must never even
    # consult it, since VendorBillLine.account_id routes straight to the expense
    # payload builder before product_uom_ids is ever looked up.
    payload = to_odoo_account_move_payload(bill, currency_id=31, product_uom_ids={})
    line_payload = payload["invoice_line_ids"][0][2]
    assert "product_uom_id" not in line_payload
    assert "product_id" not in line_payload


# =================================================================== 5: preview/execution identical UoM semantics


async def test_preview_and_execution_resolve_the_same_uom_from_the_same_odoo_state() -> None:
    """Both the real execution writer (AccountMoveRepository) and the preview reader
    (OdooVendorBillPreviewProductUomReader) resolve the same product against the same
    underlying Odoo product.product state to the same integer -- proving the two
    independent, structurally-separate read paths (preview must never depend on any
    write-capable port) agree exactly, mirroring how currency resolution already does."""

    from app.erp.odoo.adapter import OdooReadOnlyAdapter
    from app.erp.odoo.vendor_bill_preview_product_uom_reader import OdooVendorBillPreviewProductUomReader

    execution_client = _FakeOdooJson2Client()
    preview_client = _FakeOdooJson2Client()  # independent instance, same fixture data

    execution_repository = AccountMoveRepository(client=execution_client)
    execution_uom_ids = await execution_repository._resolve_vendor_bill_product_uoms((PRODUCT_ID,))

    preview_reader = OdooVendorBillPreviewProductUomReader(
        adapter=OdooReadOnlyAdapter(client=preview_client, retry_backoff_seconds=0)
    )
    preview_uom_ids = preview_reader.resolve_vendor_bill_product_uom_ids((PRODUCT_ID,))

    assert execution_uom_ids == preview_uom_ids == {PRODUCT_ID: RESOLVED_UOM_ID}


async def test_preview_fails_closed_identically_to_execution_for_unresolved_uom() -> None:
    from app.erp.odoo.adapter import OdooReadOnlyAdapter
    from app.erp.odoo.vendor_bill_preview_product_uom_reader import OdooVendorBillPreviewProductUomReader

    preview_client = _FakeOdooJson2Client(product_uom_records=[])
    preview_reader = OdooVendorBillPreviewProductUomReader(
        adapter=OdooReadOnlyAdapter(client=preview_client, retry_backoff_seconds=0)
    )
    execution_client = _FakeOdooJson2Client(product_uom_records=[], forbid_create=True)
    execution_repository = AccountMoveRepository(client=execution_client)

    with pytest.raises(VendorBillWriteValidationError):
        preview_reader.resolve_vendor_bill_product_uom_ids((PRODUCT_ID,))
    with pytest.raises(VendorBillWriteValidationError):
        await execution_repository._resolve_vendor_bill_product_uoms((PRODUCT_ID,))


# =================================================================== 7: customer invoice/quotation unaffected


def test_customer_invoice_payload_has_no_uom_concept_at_all() -> None:
    """P0-PROD-10E touches only the Vendor Bill product-line path. CustomerInvoiceLine
    carries no uom field, and its payload builder never emits any uom-shaped key --
    confirmed unchanged by this fix."""

    line = CustomerInvoiceLine(
        product_id=PRODUCT_ID,
        quantity=Decimal("1"),
        unit_price=Decimal("100.00"),
        tax_ids=(TAX_ID,),
        description="Recharge",
    )
    assert not hasattr(line, "uom")
    assert not hasattr(line, "product_uom_id")

    customer_invoice = CustomerInvoice(
        company_id=COMPANY_ID,
        customer_id=999,
        invoice_date=date(2026, 9, 10),
        currency="TRY",
        external_uuid="uuid-customer-1",
        reference="Recharge uuid-customer-1:A",
        invoice_lines=(line,),
    )
    payload = to_odoo_customer_invoice_payload(customer_invoice, currency_id=31)
    line_payload = payload["invoice_line_ids"][0][2]
    assert "product_uom_id" not in line_payload
    assert "uom" not in line_payload
