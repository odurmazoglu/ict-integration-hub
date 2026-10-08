"""PRODUCT_NOT_FOUND operator flow: map one review line to an EXISTING Odoo product from the Workbench.

Shapes follow production: ICT Bulut (partner 24) invoices whose lines carry seller codes
01.0001 / TFZP / TGDR. The real MapExistingProductUseCase writes through the REAL
OdooSupplierInfoWriter into an in-memory Odoo, and the REAL ProductMatchingEngine reads
the same supplierinfo rows -- so "reclassification removes PRODUCT_NOT_FOUND" and
"future invoices auto-match" are proven against actual matcher semantics.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.application.effective_supplier import AcceptedSupplierReader, EffectiveSupplierResolver
from app.application.exceptions.product_remediation import SupplierInfoWriteTransportError
from app.application.workbench.dto import ReviewItem, ReviewStatus
from app.application.workbench.evidence import ReviewSourceInvoiceEvidence
from app.application.workbench.exceptions import (
    ProductRemediationEligibilityError,
    ProductRemediationSupplierUnresolvedError,
    ReviewNotFoundError,
    ReviewVersionConflictError,
)
from app.application.workbench.operator_guidance import (
    GuidanceInput,
    OperatorGuidanceFacts,
    OperatorNextAction,
    UnmatchedProductLine,
    build_operator_guidance,
)
from app.application.workbench.operator_request_handlers import ProductMappingRequestHandler, product_mapping_message
from app.application.workbench.operator_request_ingestion import (
    OperatorRequest,
    OperatorRequestAction,
    OperatorRequestOutcome,
    operator_request_key,
)
from app.application.workbench.product_mapping import (
    MapExistingProductCommand,
    MapExistingProductResult,
    ProductMappingConflictError,
    ProductMappingProductInvalidError,
    ProductMappingSellerCodeMissingError,
)
from app.application.workbench.product_mapping_use_cases import MapExistingProductUseCase
from app.application.workbench.reclassification import ReviewReclassificationResult
from app.application.workbench.selected_product_resolution import ResolutionProductRecord
from app.application.workbench.supplier_resolution import SupplierResolutionMode
from app.application.workbench.write_authorization import (
    WriteAuthorizationOperationType,
    product_mapping_authorization_consumer_id,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.connectors.exceptions import ConnectorTimeoutError
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party
from app.erp.models import ProductVariant, SupplierProductCode
from app.erp.odoo.existing_supplier_info_reader import OdooExistingSupplierInfoReader
from app.erp.odoo.workbench_operator_request_reader import OdooOperatorRequestFieldMapping, OdooOperatorRequestReader
from app.erp.write.odoo_product_write_policy import OdooProductWritePolicy
from app.erp.write.odoo_supplierinfo_writer import OdooSupplierInfoRepository, OdooSupplierInfoWriter
from app.matching import PartnerMatchResult, PartnerMatchStatus, ProductMatchingEngine, ProductMatchStatus
from tests.unit.effective_supplier_support import CanonicalPartners, raw_not_found
from tests.unit.test_adr_0013_operator_request_ingestion import (
    DECIDER,
    OPERATOR,
    FakeIssuer,
    FakeReader,
    FakeRefresher,
    RecordingAsyncUseCase,
    _context,
    _workflow,
)

COMPANY = 1
ICT_BULUT = 24
DALGAKIRAN = 75
REVIEW = "review:3943f7f9-ict-bulut-8699"
ACTOR = "onur"
TFZP_DESCRIPTION = "MS Windows Server Standart Edition Lisans per VM"


# --------------------------------------------------------------------------- in-memory Odoo


class InMemoryOdoo:
    """product.product variants + product.supplierinfo, behind the JSON-2 shape the real
    supplierinfo repository uses AND the SupplierProductRepository shape the real matcher uses."""

    def __init__(self) -> None:
        self.variants: dict[int, dict[str, Any]] = {
            501: {"tmpl": 301, "name": "Windows Server Standard (VM)", "active": True, "company_id": None},
            502: {"tmpl": 302, "name": "SQL Server Standard 2 Core", "active": True, "company_id": None},
            503: {"tmpl": 303, "name": "GPU as a Service", "active": True, "company_id": 1},
            504: {"tmpl": 304, "name": "Archived product", "active": False, "company_id": None},
            505: {"tmpl": 305, "name": "Other company product", "active": True, "company_id": 2},
            # One template, two active variants (e.g. RAM sizes).
            506: {"tmpl": 306, "name": "Laptop (16 GB)", "active": True, "company_id": None},
            507: {"tmpl": 306, "name": "Laptop (32 GB)", "active": True, "company_id": None},
        }
        self.supplierinfo: list[dict[str, Any]] = [
            # Production: GPU as a Service on ICT Bulut with an EMPTY vendor product code.
            {
                "id": 3,
                "partner_id": ICT_BULUT,
                "product_tmpl_id": 303,
                "product_id": None,
                "product_code": None,
                "company_id": 1,
            },
        ]
        self.create_calls: list[dict[str, Any]] = []
        self.fail_next_create = False
        self.search_calls = 0
        #: {search call number: row} -- a row that appears in Odoo right before that read.
        self.rows_appearing: dict[int, dict[str, Any]] = {}

    # JSON-2 client shape (OdooSupplierInfoRepository)
    async def search_read(self, *, model, domain, fields, limit=20, offset=0):
        assert model == "product.supplierinfo"
        self.search_calls += 1
        if self.search_calls in self.rows_appearing:
            self.supplierinfo.append(self.rows_appearing.pop(self.search_calls))
        rows = self.supplierinfo
        for name, op, value in domain:
            if op == "=":
                rows = [r for r in rows if r.get(name) == value]
            elif op == "in":
                rows = [r for r in rows if (r.get(name) or False) in value]
        return [
            {
                key: (
                    [row[key], "x"]
                    if key in ("partner_id", "product_tmpl_id", "product_id") and row.get(key)
                    else (row.get(key) if row.get(key) is not None else False)
                )
                for key in fields
            }
            for row in rows
        ][:limit]

    async def create_supplierinfo(self, payload: dict[str, Any]) -> int:
        if self.fail_next_create:
            self.fail_next_create = False
            raise ConnectorTimeoutError("Odoo request timed out.")
        self.create_calls.append(dict(payload))
        new_id = 100 + len(self.supplierinfo)
        self.supplierinfo.append({"id": new_id, **payload, "product_id": payload.get("product_id")})
        return new_id

    # SupplierProductRepository shape (ProductMatchingEngine)
    def find_supplier_product_codes(self, *, partner_id, product_code, company_id, limit):
        return tuple(
            SupplierProductCode(
                id=r["id"],
                partner_id=r["partner_id"],
                product_code=r["product_code"],
                product_tmpl_id=r["product_tmpl_id"],
                product_id=r.get("product_id"),
                company_id=r.get("company_id"),
            )
            for r in self.supplierinfo
            if r["partner_id"] == partner_id
            and r.get("product_code") == product_code
            and r.get("company_id") in (company_id, None, False)
        )[:limit]

    def find_template_variants(self, *, product_tmpl_id, company_id, variant_id, limit):
        return tuple(
            ProductVariant(id=vid, product_tmpl_id=v["tmpl"], active=v["active"], company_id=v["company_id"])
            for vid, v in self.variants.items()
            if v["tmpl"] == product_tmpl_id and (variant_id is None or vid == variant_id)
        )[:limit]

    # SelectedProductReader shape
    def find_products_by_ids(self, product_ids):
        return tuple(
            ResolutionProductRecord(
                id=pid,
                name=self.variants[pid]["name"],
                default_code=None,
                barcode=None,
                active=self.variants[pid]["active"],
                company_id=self.variants[pid]["company_id"],
                product_tmpl_id=self.variants[pid]["tmpl"],
            )
            for pid in product_ids
            if pid in self.variants
        )


class _NoGlobalProducts:
    """ICT product identities (default_code); empty by default -- only supplier mappings match."""

    def __init__(self, default_codes: dict[str, int] | None = None) -> None:
        self.default_codes = default_codes or {}

    def find_by_default_code(self, value, *, company_id=None):
        from app.erp.models import Product

        product_id = self.default_codes.get(value)
        if product_id is None:
            return ()
        return (Product(id=product_id, name="x", default_code=value, barcode=None, active=True),)

    def find_by_barcode(self, value, *, company_id=None):
        return ()


def _matcher(odoo: InMemoryOdoo, default_codes: dict[str, int] | None = None) -> ProductMatchingEngine:
    return ProductMatchingEngine(
        SimpleNamespace(product_repository=_NoGlobalProducts(default_codes)), supplier_product_repository=odoo
    )


def _line(number: str, code: str | None, description: str, quantity: str = "1") -> InvoiceLine:
    return InvoiceLine(
        line_number=number,
        description=description,
        quantity=Decimal(quantity),
        unit_code="C62",
        unit_price=Decimal("10"),
        seller_item_code=code,
    )


def _invoice(*lines: InvoiceLine, number: str = "ICF2026000008699") -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number=number,
            invoice_uuid="44238F65-9B68-44C6-AFA6-CFF11E4B835D",
            ettn=number,
            issue_date=date(2026, 9, 30),
            currency_code="TRY",
        ),
        supplier=Party(name="ICT BULUT BİLİŞİM A.Ş.", tax_number="4650459971"),
        customer=Party(name="ICT", tax_number="1112223334"),
        totals=MonetaryTotals(payable_amount=Decimal("100")),
        lines=lines,
    )


def _matched(partner_id: int) -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MATCHED,
        partner_id=partner_id,
        matched_by="tax_number",
        reason="x",
        candidate_count=1,
        confidence=Decimal("1.00"),
    )


def _product_reasons(invoice: InternalInvoice, odoo: InMemoryOdoo, partner_id: int) -> tuple[ManualReviewReason, ...]:
    result = _matcher(odoo).match_invoice(invoice, company_id=COMPANY, partner_match=_matched(partner_id))
    return tuple(
        ManualReviewReason(
            code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
            message="Product was not matched.",
            line_number=item.line_number,
            source="product_matching",
        )
        for item in result.line_results
        if item.result.status is not ProductMatchStatus.MATCHED
    )


class Harness:
    """Review state + a matcher-backed reclassifier over the same in-memory Odoo."""

    def __init__(
        self,
        invoice: InternalInvoice,
        *,
        partner_match: PartnerMatchResult | None = None,
        claim: Any = None,
        odoo: InMemoryOdoo | None = None,
    ) -> None:
        self.odoo = odoo or InMemoryOdoo()
        self.invoice = invoice
        self.partner_match = partner_match or _matched(ICT_BULUT)
        self.review = ReviewItem(
            review_id=REVIEW,
            invoice_id=invoice.header.ettn or "x",
            invoice_number=invoice.header.invoice_number,
            supplier_tax_number="4650459971",
            supplier_name="ICT BULUT BİLİŞİM A.Ş.",
            invoice_date=date(2026, 9, 30),
            currency="TRY",
            total_amount=Decimal("100"),
            workflow=WorkflowType.MANUAL_REVIEW,
            status=ReviewStatus.PENDING_REVIEW,
            review_reasons=_product_reasons(invoice, self.odoo, self.partner_match.partner_id),
            version=2,
        )
        self.claim = claim
        #: No accepted supplier resolution unless a test records one (PR A).
        self.effect: Any = None
        self.partners = CanonicalPartners()
        self.accepted_supplier_reader = AcceptedSupplierReader(effect_reader=self, partner_reader=self.partners)
        self.commits = 0
        self.rollbacks = 0
        self.reclassify_calls: list[Any] = []
        self.reclassify_error: Exception | None = None
        self.consumed: list[dict[str, Any]] = []
        self.use_case = MapExistingProductUseCase(
            review_reader=self,
            source_invoice_reader=self,
            effective_supplier_resolver=EffectiveSupplierResolver(
                partner_matcher=self, accepted_supplier_reader=self.accepted_supplier_reader
            ),
            product_reader=self.odoo,
            identity_claim_reader=self,
            existing_supplier_info_reader=OdooExistingSupplierInfoReader(
                repository=OdooSupplierInfoRepository(client=self.odoo)
            ),
            supplier_info_writer=OdooSupplierInfoWriter(
                repository=OdooSupplierInfoRepository(client=self.odoo),
                policy=OdooProductWritePolicy(
                    product_remediation_write_enabled=True, app_env="staging", odoo_host="test-ictteknoloji.odoo.com"
                ),
            ),
            reclassifier=self,
            unit_of_work=self,
            write_authorization_repository=self,
        )

    # ports
    def get_review_item(self, query):
        return self.review

    def get(self, *, review_id, company_id):
        return ReviewSourceInvoiceEvidence(
            review_id=REVIEW,
            company_id=COMPANY,
            review_version=1,
            source_invoice_id=self.invoice.header.ettn,
            invoice=self.invoice,
        )

    def match_invoice(self, invoice, *, company_id=None):
        """Raw deterministic partner match (PartnerMatchingEngine shape)."""
        return self.partner_match or raw_not_found()

    def find_latest_remediation_effect(self, *, review_id, company_id):
        return self.effect

    def find(self, *, company_id, resolved_supplier_partner_id, seller_item_code):
        return self.claim

    def claim_and_consume(self, **kwargs):
        self.consumed.append(kwargs)
        return None  # policy is enabled in this harness; the record itself is not needed

    async def execute(self, command):
        self.reclassify_calls.append(command)
        if self.reclassify_error is not None:
            error, self.reclassify_error = self.reclassify_error, None
            raise error
        previous = self.review
        self.review = replace(
            previous,
            version=previous.version + 1,
            review_reasons=_product_reasons(self.invoice, self.odoo, self.supplier_id),
        )
        return ReviewReclassificationResult(
            review_id=REVIEW,
            company_id=COMPANY,
            changed=True,
            from_version=previous.version,
            to_version=self.review.version,
            previous_workflow=previous.workflow,
            new_workflow=self.review.workflow,
            previous_review_reasons=previous.review_reasons,
            new_review_reasons=self.review.review_reasons,
            trigger=command.trigger,
            executable=False,
        )

    @property
    def supplier_id(self) -> int:
        return self.partner_match.partner_id if self.partner_match is not None else ICT_BULUT

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def command(self, line: str, product_id: int, **overrides) -> MapExistingProductCommand:
        values = {
            "review_id": REVIEW,
            "company_id": COMPANY,
            "expected_version": self.review.version,
            "line_number": line,
            "product_id": product_id,
            "approved_by": ACTOR,
        }
        values.update(overrides)
        return MapExistingProductCommand(**values)

    def new_rows(self) -> list[dict[str, Any]]:
        return [row for row in self.odoo.supplierinfo if row["id"] != 3]


def _ict_8699() -> InternalInvoice:
    return _invoice(
        _line("1", "TFZP", TFZP_DESCRIPTION, "2"), _line("2", "TGDR", "MS SQL Server Standart Edition 2 Core Lisans")
    )


def _lines_with_reason(review: ReviewItem) -> list[str]:
    return [r.line_number for r in review.review_reasons if r.code is ManualReviewReasonCode.PRODUCT_NOT_FOUND]


# --------------------------------------------------------------------------- 1-4: map, reclassify, auto-match


async def test_one_line_maps_to_an_existing_product_and_reclassification_clears_only_that_line() -> None:
    h = Harness(_ict_8699())
    assert _lines_with_reason(h.review) == ["1", "2"]

    result = await h.use_case.execute(h.command("1", 501))

    assert h.odoo.create_calls == [
        {
            "partner_id": ICT_BULUT,
            "product_tmpl_id": 301,
            "product_code": "TFZP",
            "company_id": COMPANY,
            "product_id": 501,
            "product_name": TFZP_DESCRIPTION,
        }
    ]
    assert result.line_resolved is True and result.remaining_product_lines == ("2",)
    assert (result.previous_version, result.current_version) == (2, 3)
    assert _lines_with_reason(h.review) == ["2"]
    assert h.commits == 1 and h.rollbacks == 0
    note = h.reclassify_calls[0].note
    assert "line 1" in note and "'TFZP'" in note and "product.product 501" in note and ACTOR in note


async def test_identical_existing_mapping_is_reused_without_any_odoo_write() -> None:
    h = Harness(_ict_8699())
    h.odoo.supplierinfo.append(
        {
            "id": 77,
            "partner_id": ICT_BULUT,
            "product_tmpl_id": 301,
            "product_id": None,
            "product_code": "TFZP",
            "company_id": None,
        }
    )

    result = await h.use_case.execute(h.command("1", 501))

    assert h.odoo.create_calls == []
    assert result.supplierinfo_id == 77 and result.created_supplierinfo is False and result.line_resolved is True
    assert h.consumed == []  # no write -> no authorization consumed


async def test_future_invoice_from_the_same_supplier_auto_matches_without_operator() -> None:
    h = Harness(_ict_8699())
    await h.use_case.execute(h.command("1", 501))

    next_invoice = _invoice(_line("1", "TFZP", "Windows Server lisansı (Ekim)"), number="ICF2026000009999")
    match = _matcher(h.odoo).match_invoice(next_invoice, company_id=COMPANY, partner_match=_matched(ICT_BULUT))

    assert match.line_results[0].result.status is ProductMatchStatus.MATCHED
    assert match.line_results[0].result.product_id == 501


async def test_same_seller_code_from_a_different_supplier_never_reuses_the_mapping() -> None:
    h = Harness(_ict_8699())
    await h.use_case.execute(h.command("1", 501))

    other = _invoice(_line("1", "TFZP", "Bambaşka bir ürün"), number="D012026000009999")
    match = _matcher(h.odoo).match_invoice(other, company_id=COMPANY, partner_match=_matched(DALGAKIRAN))

    assert match.line_results[0].result.status is ProductMatchStatus.NOT_FOUND


# --------------------------------------------------------------------------- 6: several lines


async def test_two_unmatched_lines_are_resolved_independently() -> None:
    h = Harness(_ict_8699())

    first = await h.use_case.execute(h.command("1", 501))
    second = await h.use_case.execute(h.command("2", 502))

    assert first.remaining_product_lines == ("2",)
    assert second.line_resolved is True and second.remaining_product_lines == ()
    assert [(row["product_code"], row["product_tmpl_id"]) for row in h.new_rows()] == [("TFZP", 301), ("TGDR", 302)]
    assert h.review.review_reasons == () and h.review.version == 4


# --------------------------------------------------------------------------- 7-9: conflict, replay, stale


async def test_existing_mapping_to_a_different_product_fails_closed_and_changes_nothing() -> None:
    h = Harness(_ict_8699())
    h.odoo.supplierinfo.append(
        {
            "id": 78,
            "partner_id": ICT_BULUT,
            "product_tmpl_id": 302,
            "product_id": None,
            "product_code": "TFZP",
            "company_id": 1,
        }
    )

    with pytest.raises(ProductMappingConflictError) as caught:
        await h.use_case.execute(h.command("1", 501))

    assert h.odoo.create_calls == [] and h.reclassify_calls == [] and h.commits == 0
    assert "zaten başka bir ürüne eşlenmiş" in caught.value.safe_message and "#78" in caught.value.safe_message


async def test_recorded_create_new_product_for_the_same_identity_fails_closed() -> None:
    h = Harness(_ict_8699(), claim=object())

    with pytest.raises(ProductMappingConflictError):
        await h.use_case.execute(h.command("1", 501))
    assert h.odoo.create_calls == []


async def test_double_click_and_crash_resume_never_duplicate_the_mapping() -> None:
    h = Harness(_ict_8699())
    h.reclassify_error = RuntimeError("crash after the Odoo write, before reclassification")
    with pytest.raises(RuntimeError):
        await h.use_case.execute(h.command("1", 501))
    assert len(h.new_rows()) == 1 and h.rollbacks == 1  # Odoo row exists; Hub transaction rolled back

    resumed = await h.use_case.execute(h.command("1", 501))  # same request retried
    with pytest.raises(ReviewVersionConflictError):
        await h.use_case.execute(h.command("1", 501, expected_version=2))  # second click on the old version

    assert resumed.created_supplierinfo is False and resumed.line_resolved is True
    assert len(h.new_rows()) == 1 and len(h.odoo.create_calls) == 1


async def test_stale_review_version_is_rejected_before_any_read_or_write() -> None:
    h = Harness(_ict_8699())

    with pytest.raises(ReviewVersionConflictError) as caught:
        await h.use_case.execute(h.command("1", 501, expected_version=1))
    assert h.odoo.create_calls == [] and "güncel" in caught.value.safe_message.lower()


# --------------------------------------------------------------------------- 10-11: missing code, Odoo failure


async def test_line_without_seller_code_is_refused_and_no_code_is_invented() -> None:
    h = Harness(_invoice(_line("1", None, "İADE")))
    h.review = replace(
        h.review,
        review_reasons=(
            ManualReviewReason(code=ManualReviewReasonCode.PRODUCT_NOT_FOUND, message="x", line_number="1"),
        ),
    )

    with pytest.raises(ProductMappingSellerCodeMissingError) as caught:
        await h.use_case.execute(h.command("1", 501))
    assert h.odoo.create_calls == []
    assert "satıcı ürün kodu yok" in caught.value.safe_message


async def test_odoo_write_failure_rolls_back_and_a_retry_succeeds_once() -> None:
    h = Harness(_ict_8699())
    h.odoo.fail_next_create = True

    with pytest.raises(SupplierInfoWriteTransportError):
        await h.use_case.execute(h.command("1", 501))
    assert h.new_rows() == [] and h.reclassify_calls == [] and h.commits == 0 and h.rollbacks == 1

    result = await h.use_case.execute(h.command("1", 501))
    assert result.line_resolved is True and len(h.new_rows()) == 1


@pytest.mark.parametrize(
    ("product_id", "message"),
    [(999, "bulunamadı"), (504, "arşivlenmiş"), (505, "başka bir şirkete")],
)
async def test_missing_archived_or_foreign_product_is_refused(product_id: int, message: str) -> None:
    h = Harness(_ict_8699())
    with pytest.raises(ProductMappingProductInvalidError) as caught:
        await h.use_case.execute(h.command("1", product_id))
    assert message in caught.value.safe_message and h.odoo.create_calls == []


@pytest.mark.parametrize(
    "partner_match",
    [
        None,
        PartnerMatchResult(
            status=PartnerMatchStatus.MULTIPLE_MATCHES,
            partner_id=None,
            matched_by=None,
            reason="x",
            candidate_count=2,
            confidence=None,
        ),
    ],
)
async def test_unresolved_supplier_is_refused(partner_match) -> None:
    h = Harness(_ict_8699())
    h.partner_match = partner_match
    with pytest.raises(ProductRemediationSupplierUnresolvedError):
        await h.use_case.execute(h.command("1", 501))
    assert h.odoo.create_calls == []


async def test_line_without_product_not_found_is_not_eligible() -> None:
    h = Harness(_ict_8699())
    with pytest.raises(ProductRemediationEligibilityError):
        await h.use_case.execute(h.command("9", 501))


async def test_authorization_is_consumed_for_exactly_this_line_and_operation() -> None:
    h = Harness(_ict_8699())
    await h.use_case.execute(h.command("1", 501, authorization_id="auth-1"))

    assert h.consumed == [
        {
            "company_id": COMPANY,
            "review_id": REVIEW,
            "operation_type": WriteAuthorizationOperationType.MAP_EXISTING_PRODUCT,
            "target_version": 2,
            "authorization_id": "auth-1",
            "execution_id": product_mapping_authorization_consumer_id(
                company_id=COMPANY, review_id=REVIEW, expected_version=2, line_number="1"
            ),
        }
    ]


# --------------------------------------------------------------------------- 12: operator text + guidance


def _result(**overrides) -> MapExistingProductResult:
    values = dict(
        review_id=REVIEW,
        company_id=COMPANY,
        line_number="1",
        seller_item_code="TFZP",
        supplier_partner_id=ICT_BULUT,
        product_id=501,
        product_name="Windows Server Standard (VM)",
        supplierinfo_id=103,
        created_supplierinfo=True,
        previous_version=2,
        current_version=3,
        line_resolved=True,
        remaining_product_lines=("2",),
    )
    values.update(overrides)
    return MapExistingProductResult(**values)


def test_operator_result_message_is_turkish_and_names_the_mapping() -> None:
    assert product_mapping_message(_result()) == (
        "Ürün eşleştirildi: Satır 1 (satıcı kodu TFZP) → Windows Server Standard (VM). Bu tedarikçinin sonraki "
        "faturalarında otomatik eşleşecek. Kalan eşleştirilecek satırlar: 2."
    )
    assert "teknik destek" in product_mapping_message(_result(line_resolved=False))


def _guidance(*lines: UnmatchedProductLine, enabled: bool = True, codes=(ManualReviewReasonCode.PRODUCT_NOT_FOUND,)):
    source = GuidanceInput(
        status=ReviewStatus.PENDING_REVIEW,
        reason_codes=tuple(codes),
        supplier_name="ICT BULUT",
        decision_type=None,
        decision_workflow=None,
        decision_version=None,
        execution_state=None,
        vendor_bill_id=None,
    )
    return build_operator_guidance(
        source, OperatorGuidanceFacts(product_mapping_enabled=enabled, unmatched_product_lines=lines)
    )


def test_guidance_lists_each_unmatched_line_for_the_operator() -> None:
    guidance = _guidance(
        UnmatchedProductLine("1", "TFZP", TFZP_DESCRIPTION, "2", "C62"),
        UnmatchedProductLine("3", None, "İADE", "1", "C62"),
    )

    assert guidance.next_action is OperatorNextAction.PRODUCT
    assert guidance.next_action.value == "Ürün Eşleştirmesi Yapılmalı"
    assert "Satır 1 — Satıcı Ürün Kodu: TFZP — MS Windows Server Standart Edition Lisans per VM — Miktar: 2 C62" in (
        guidance.todo_html
    )
    assert "Satır 3 — satıcı ürün kodu yok" in guidance.todo_html
    assert "Ürün Eşleştir" in guidance.todo_html and "✓ Tedarikçi: ICT BULUT" in guidance.completed_html


def test_guidance_is_unchanged_until_the_request_fields_are_provisioned() -> None:
    guidance = _guidance(UnmatchedProductLine("1", "TFZP"), enabled=False)
    assert guidance.next_action is OperatorNextAction.TECHNICAL


def test_lines_without_any_seller_code_stay_a_technical_escalation() -> None:
    guidance = _guidance(UnmatchedProductLine("1", None, "İADE"))
    assert guidance.next_action is OperatorNextAction.TECHNICAL


def test_supplier_step_still_comes_before_product_mapping() -> None:
    guidance = _guidance(
        UnmatchedProductLine("1", "TFZP"),
        codes=(ManualReviewReasonCode.SUPPLIER_NOT_FOUND, ManualReviewReasonCode.PRODUCT_NOT_FOUND),
    )
    assert guidance.next_action is OperatorNextAction.SUPPLIER


# --------------------------------------------------------------------------- 13-14: Workbench request path


REQUESTED_AT = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


def _mapping_request(**overrides) -> OperatorRequest:
    values = dict(
        odoo_record_id=23,
        review_id=REVIEW,
        company_id=COMPANY,
        action=OperatorRequestAction.PRODUCT_MAPPING,
        expected_version=2,
        requested_by_odoo_user_id=2,
        requested_at=REQUESTED_AT,
        line_number="1",
        product_id=501,
    )
    values.update(overrides)
    return OperatorRequest(**values)


class _Adapter:
    def __init__(self, rows):
        self.rows = rows

    def search_read(self, **kwargs):
        return tuple(self.rows)

    def write(self, **kwargs):
        raise AssertionError("the reader never writes")


def _request_mapping(**overrides) -> OdooOperatorRequestFieldMapping:
    values = dict(
        model="x_ipp_import_workbench",
        review_id="x_studio_review_id",
        company_id="x_studio_company",
        ready="x_studio_ipp_req_ready",
        action="x_studio_ipp_req_action",
        expected_version="x_studio_ipp_req_version",
        requested_by="x_studio_ipp_req_requested_by",
        requested_at="x_studio_ipp_req_requested_at",
        result="x_studio_ipp_req_result",
        message="x_studio_ipp_req_message",
        processed_at="x_studio_ipp_req_processed_at",
        line="x_studio_ipp_req_line",
        product="x_studio_ipp_req_product",
    )
    values.update(overrides)
    return OdooOperatorRequestFieldMapping(**values)


def test_odoo_row_parses_into_a_line_specific_product_mapping_request() -> None:
    row = {
        "id": 23,
        "x_studio_review_id": REVIEW,
        "x_studio_company": [1, "ICT"],
        "x_studio_ipp_req_ready": True,
        "x_studio_ipp_req_action": "Ürün Eşleştir",
        "x_studio_ipp_req_version": 2,
        "x_studio_ipp_req_requested_by": [2, "Onur"],
        "x_studio_ipp_req_requested_at": "2026-10-07 09:00:00",
        "x_studio_ipp_req_line": " 1 ",
        "x_studio_ipp_req_product": [501, "Windows Server Standard (VM)"],
    }

    (request,) = OdooOperatorRequestReader(adapter=_Adapter([row]), mapping=_request_mapping()).list_pending(
        company_id=COMPANY, limit=10
    )

    assert request == _mapping_request()
    assert "x_studio_ipp_req_line" in _request_mapping().read_fields()
    assert _request_mapping().product_mapping_enabled is True
    assert _request_mapping(line=None).product_mapping_enabled is False


def test_product_mapping_request_without_line_or_product_is_a_readable_refusal() -> None:
    row = {
        "id": 23,
        "x_studio_review_id": REVIEW,
        "x_studio_company": [1, "ICT"],
        "x_studio_ipp_req_ready": True,
        "x_studio_ipp_req_action": "Ürün Eşleştir",
        "x_studio_ipp_req_version": 2,
        "x_studio_ipp_req_requested_by": [2, "Onur"],
        "x_studio_ipp_req_requested_at": "2026-10-07 09:00:00",
        "x_studio_ipp_req_product": [501, "x"],
    }

    (failure,) = OdooOperatorRequestReader(adapter=_Adapter([row]), mapping=_request_mapping()).list_pending(
        company_id=COMPANY, limit=10
    )

    assert failure.message == "Eşleştirilecek fatura satırı seçilmelidir."


def test_request_key_is_line_and_product_specific_and_unchanged_for_existing_actions() -> None:
    assert operator_request_key(_mapping_request()) != operator_request_key(_mapping_request(line_number="2"))
    assert operator_request_key(_mapping_request()) != operator_request_key(_mapping_request(product_id=502))

    supplier = _mapping_request(
        action=OperatorRequestAction.SUPPLIER_RESOLUTION,
        line_number=None,
        product_id=None,
        supplier_mode=SupplierResolutionMode.MATCH_EXISTING,
        partner_id=24,
    )
    legacy_payload = {  # the pre-existing key payload, byte for byte
        "company_id": 1,
        "review_id": REVIEW,
        "odoo_record_id": 23,
        "action": "supplier_resolution",
        "expected_version": 2,
        "requested_by": 2,
        "requested_at": REQUESTED_AT.isoformat(),
        "supplier_mode": "match_existing",
        "partner_id": 24,
        "purchase_purpose": None,
        "treatment_type": None,
        "expense_account_id": None,
        "expense_category": None,
        "asset_account_id": None,
        "depreciation_model_id": None,
        "note": None,
    }
    encoded = json.dumps(legacy_payload, sort_keys=True, separators=(",", ":"))
    assert operator_request_key(supplier) == f"odoo-operator-request:{hashlib.sha256(encoded.encode()).hexdigest()}"


def test_handler_issues_the_narrow_authorization_and_reports_the_mapping() -> None:
    use_case = RecordingAsyncUseCase(_result(remaining_product_lines=()))
    issued: list[str] = []

    outcome = ProductMappingRequestHandler(use_case=use_case).handle(_mapping_request(), _context(issued=issued))

    assert issued == ["MAP_EXISTING_PRODUCT"]
    assert use_case.commands == [
        MapExistingProductCommand(
            review_id=REVIEW,
            company_id=COMPANY,
            expected_version=2,
            line_number="1",
            product_id=501,
            approved_by="operator",
            authorization_id="auth-1",
        )
    ]
    assert outcome.outcome is OperatorRequestOutcome.COMPLETED
    assert outcome.message.startswith("Ürün eşleştirildi: Satır 1")


def test_workbench_request_runs_through_the_hub_pull_pipeline_and_refreshes_the_projection() -> None:
    use_case = RecordingAsyncUseCase(_result(remaining_product_lines=()))
    refresher, issuer = FakeRefresher(), FakeIssuer()
    workflow = _workflow(
        FakeReader([_mapping_request()]),
        ProductMappingRequestHandler(use_case=use_case),
        refresher=refresher,
        issuer=issuer,
        action=OperatorRequestAction.PRODUCT_MAPPING,
    )

    (result,) = workflow.run(company_id=COMPANY).results

    assert result.outcome is OperatorRequestOutcome.COMPLETED
    assert [call["operation_type"] for call in issuer.calls] == ["MAP_EXISTING_PRODUCT"]
    assert refresher.calls == [(REVIEW, COMPANY)]


def test_operator_without_execute_permission_cannot_write_a_mapping() -> None:
    use_case = RecordingAsyncUseCase(_result())
    workflow = _workflow(
        FakeReader([_mapping_request()]),
        ProductMappingRequestHandler(use_case=use_case),
        actors={2: DECIDER},
        action=OperatorRequestAction.PRODUCT_MAPPING,
    )

    (result,) = workflow.run(company_id=COMPANY).results

    assert result.outcome is OperatorRequestOutcome.REJECTED and use_case.commands == []


def test_unprovisioned_product_mapping_is_rejected_not_guessed() -> None:
    workflow = _workflow(
        FakeReader([_mapping_request()]),
        RecordingAsyncUseCase(None),  # type: ignore[arg-type]
        action=OperatorRequestAction.PURCHASE_PURPOSE,
    )

    (result,) = workflow.run(company_id=COMPANY).results

    assert result.outcome is OperatorRequestOutcome.REJECTED and result.message == "Bu işlem henüz desteklenmiyor."


def test_there_is_no_direct_odoo_to_hub_endpoint_for_product_mapping() -> None:
    from app.api.routers.workbench import router

    paths = {route.path for route in router.routes}
    assert not any("mapping" in path and "product" in path for path in paths)
    assert OPERATOR.allows(frozenset({"workbench_review_decide", "workbench_execute"}))


# --------------------------------------------------------------------------- composition


def test_production_composition_registers_the_handler_only_when_both_fields_are_mapped() -> None:
    from unittest.mock import MagicMock

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.composition.operator_requests import build_operator_request_workflow
    from app.core.config import Settings
    from tests.unit.test_odoo_supplier_partner_writer import FakeJson2Client

    session = Session(bind=create_engine("sqlite://"))
    registered = []
    for mapping in (_request_mapping(), _request_mapping(line=None), _request_mapping(product=None)):
        workflow = build_operator_request_workflow(
            business_session=session,
            ledger_session=session,
            settings=Settings(),
            odoo_client=FakeJson2Client(),
            request_mapping=mapping,
            decision_mapping=MagicMock(),
        )
        handler = workflow._handlers.get(OperatorRequestAction.PRODUCT_MAPPING)
        registered.append(type(handler).__name__ if handler else None)

    assert registered == ["ProductMappingRequestHandler", None, None]


def test_guidance_line_list_comes_from_the_immutable_source_and_degrades_safely() -> None:
    from app.composition.imports import _unmatched_product_lines

    h = Harness(
        _invoice(_line("1", " TFZP ", TFZP_DESCRIPTION, "2.000"), _line("2", "TGDR", "SQL"), _line("3", None, "İADE"))
    )
    h.review = replace(
        h.review,
        review_reasons=tuple(
            ManualReviewReason(code=ManualReviewReasonCode.PRODUCT_NOT_FOUND, message="x", line_number=n)
            for n in ("1", "3")
        ),
    )

    lines = _unmatched_product_lines(h.review, COMPANY, h)

    assert lines == (
        UnmatchedProductLine("1", "TFZP", TFZP_DESCRIPTION, "2", "C62"),
        UnmatchedProductLine("3", None, "İADE", "1", "C62"),
    )

    class _Broken:
        def get(self, **kwargs):
            raise ReviewNotFoundError("Review source evidence was not found.")

    assert _unmatched_product_lines(h.review, COMPANY, _Broken()) == ()


def test_product_mapping_guidance_switch_follows_the_operator_request_configuration(monkeypatch) -> None:
    from app.composition.imports import _product_mapping_requests_enabled
    from app.core.config import Settings

    for key, value in {
        "PARENT_MODEL": "x_ipp_import_workbench",
        "REVIEW_ID_FIELD": "x_studio_review_id",
        "COMPANY_ID_FIELD": "x_studio_company",
        "READY_FIELD": "r",
        "ACTION_FIELD": "a",
        "EXPECTED_VERSION_FIELD": "v",
        "REQUESTED_BY_FIELD": "b",
        "REQUESTED_AT_FIELD": "t",
        "RESULT_FIELD": "s",
        "MESSAGE_FIELD": "m",
        "PROCESSED_AT_FIELD": "p",
    }.items():
        monkeypatch.setenv(f"ODOO_WORKBENCH_REQUEST_{key}", value)
    enabled = Settings(odoo_workbench_operator_requests_enabled=True)

    assert _product_mapping_requests_enabled(enabled) is False  # fields not mapped yet
    monkeypatch.setenv("ODOO_WORKBENCH_REQUEST_LINE_FIELD", "x_studio_ipp_req_line")
    monkeypatch.setenv("ODOO_WORKBENCH_REQUEST_PRODUCT_FIELD", "x_studio_ipp_req_product")
    assert _product_mapping_requests_enabled(enabled) is True
    assert _product_mapping_requests_enabled(Settings(odoo_workbench_operator_requests_enabled=False)) is False


# --------------------------------------------------------------------------- final-review proofs (PR #208)


async def test_multi_variant_template_maps_and_future_invoices_resolve_the_exact_selected_variant() -> None:
    h = Harness(_invoice(_line("1", "LAP-32", "Dizüstü bilgisayar 32 GB")))

    result = await h.use_case.execute(h.command("1", 507))

    assert h.odoo.create_calls[0]["product_tmpl_id"] == 306 and h.odoo.create_calls[0]["product_id"] == 507
    assert result.line_resolved is True
    future = _invoice(_line("1", "LAP-32", "Dizüstü"), number="ICF2026000010000")
    resolved = _matcher(h.odoo).match_invoice(future, company_id=COMPANY, partner_match=_matched(ICT_BULUT))
    assert resolved.line_results[0].result.product_id == 507  # never the sibling variant 506


async def test_existing_template_level_row_on_a_multi_variant_template_is_reused_never_repointed() -> None:
    h = Harness(_invoice(_line("1", "LAP-32", "Dizüstü bilgisayar 32 GB")))
    h.odoo.supplierinfo.append(
        {
            "id": 90,
            "partner_id": ICT_BULUT,
            "product_tmpl_id": 306,
            "product_id": None,
            "product_code": "LAP-32",
            "company_id": 1,
        }
    )

    result = await h.use_case.execute(h.command("1", 507))

    assert h.odoo.create_calls == []  # no second row next to the template-level one
    assert result.supplierinfo_id == 90 and result.line_resolved is False  # two variants: not provable
    assert "hâlâ eşleşmedi" in product_mapping_message(result)


async def test_existing_row_pinning_a_sibling_variant_is_a_conflict() -> None:
    h = Harness(_invoice(_line("1", "LAP-32", "Dizüstü bilgisayar 32 GB")))
    h.odoo.supplierinfo.append(
        {
            "id": 91,
            "partner_id": ICT_BULUT,
            "product_tmpl_id": 306,
            "product_id": 506,
            "product_code": "LAP-32",
            "company_id": 1,
        }
    )

    with pytest.raises(ProductMappingConflictError):
        await h.use_case.execute(h.command("1", 507))
    assert h.odoo.create_calls == []


async def test_sibling_variant_mapping_created_concurrently_is_not_reported_as_this_mapping() -> None:
    h = Harness(_invoice(_line("1", "LAP-32", "Dizüstü bilgisayar 32 GB")))
    # Pre-check (read 1) sees nothing; the row appears before the writer's own read (read 2).
    h.odoo.rows_appearing[2] = {
        "id": 92,
        "partner_id": ICT_BULUT,
        "product_tmpl_id": 306,
        "product_id": 506,
        "product_code": "LAP-32",
        "company_id": 1,
    }

    with pytest.raises(ProductMappingConflictError) as caught:
        await h.use_case.execute(h.command("1", 507))

    assert h.odoo.create_calls == [] and h.reclassify_calls == [] and h.rollbacks == 1
    assert "başka bir varyantına" in caught.value.safe_message


async def test_repeated_line_number_on_the_source_invoice_is_refused() -> None:
    h = Harness(_invoice(_line("1", "TFZP", TFZP_DESCRIPTION), _line("1", "TGDR", "SQL")))

    with pytest.raises(ProductRemediationEligibilityError) as caught:
        await h.use_case.execute(h.command("1", 501))
    assert "birden fazla satır" in caught.value.safe_message and h.odoo.create_calls == []


async def test_seller_code_is_whitespace_trimmed_and_case_sensitive_on_both_write_and_read() -> None:
    h = Harness(_invoice(_line("1", "  TFZP  ", TFZP_DESCRIPTION)))
    await h.use_case.execute(h.command("1", 501))

    assert h.odoo.create_calls[0]["product_code"] == "TFZP"
    matcher = _matcher(h.odoo)

    def first(code: str):
        invoice = _invoice(_line("1", code, "x"), number="ICF2026000010001")
        return matcher.match_invoice(invoice, company_id=COMPANY, partner_match=_matched(ICT_BULUT)).line_results[0]

    assert first(" TFZP").result.product_id == 501
    assert first("tfzp").result.status is ProductMatchStatus.NOT_FOUND


async def test_two_suppliers_keep_independent_mappings_for_the_same_seller_code() -> None:
    odoo = InMemoryOdoo()
    ict = Harness(_invoice(_line("1", "TFZP", "ICT Bulut ürünü")), odoo=odoo)
    other = Harness(
        _invoice(_line("1", "TFZP", "Dalgakıran ürünü"), number="D012026000006193"),
        partner_match=_matched(DALGAKIRAN),
        odoo=odoo,
    )

    await ict.use_case.execute(ict.command("1", 501))
    await other.use_case.execute(other.command("1", 502))

    def resolved(partner: int) -> int | None:
        invoice = _invoice(_line("1", "TFZP", "x"), number="ICF2026000010003")
        lines = _matcher(odoo).match_invoice(invoice, company_id=COMPANY, partner_match=_matched(partner))
        return lines.line_results[0].result.product_id

    assert (resolved(ICT_BULUT), resolved(DALGAKIRAN)) == (501, 502)


def test_unmapped_seller_code_equal_to_a_default_code_is_not_a_global_match() -> None:
    """Finding 7, resolved: an UNMAPPED seller code equal to an ICT default_code is advisory only."""

    invoice = _invoice(_line("1", "CFQ7TTC0LH18:0001", "Microsoft 365 Business Basic"))
    lines = _matcher(InMemoryOdoo(), {"CFQ7TTC0LH18:0001": 393}).match_invoice(
        invoice, company_id=COMPANY, partner_match=_matched(ICT_BULUT)
    )
    result = lines.line_results[0].result

    assert (result.status, result.product_id, result.matched_by) == (ProductMatchStatus.NOT_FOUND, None, None)


async def test_a_mapping_is_never_overridden_by_a_conflicting_global_default_code() -> None:
    h = Harness(_ict_8699())
    await h.use_case.execute(h.command("1", 501))
    future = _invoice(_line("1", "TFZP", "x"), number="ICF2026000010004")

    def matched(default_codes: dict[str, int]):
        lines = _matcher(h.odoo, default_codes).match_invoice(
            future, company_id=COMPANY, partner_match=_matched(ICT_BULUT)
        )
        return lines.line_results[0].result

    conflicting, agreeing = matched({"TFZP": 999}), matched({"TFZP": 501})
    assert conflicting.status is ProductMatchStatus.MATCHED and conflicting.product_id == 501
    assert agreeing.status is ProductMatchStatus.MATCHED and agreeing.product_id == 501
