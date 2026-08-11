from __future__ import annotations

import inspect
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.application.workbench.billing_capture_use_cases as capture_use_cases
from app.application.workbench import (
    AllocationCompleteness,
    BusinessContextAllocation,
    BusinessContextAllocationSet,
    BusinessContextAllocationType,
    CaptureOdooWorkbenchBillingEvidenceCommand,
    CaptureOdooWorkbenchBillingEvidenceUseCase,
    CurrencyReference,
    OdooWorkbenchDecisionCandidate,
    PartnerReference,
    ProductReference,
    ReviewDecisionType,
    ReviewItem,
    ReviewStatus,
    SalesTaxReference,
    WorkbenchBillingAuthoringRow,
    WorkbenchBillingReferenceValidator,
    WorkbenchCandidateAmbiguityError,
    WorkbenchCandidateDataError,
    WorkbenchCandidateNotFoundError,
    WorkbenchErpReferenceCompanyMismatchError,
    WorkbenchErpReferenceNotFoundError,
    WorkbenchErpReferenceTypeError,
)
from app.application.workbench.exceptions import (
    ReviewDataIntegrityError,
    ReviewIdempotencyConflictError,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.db.base import Base
from app.erp.odoo.workbench_billing_authoring_reader import (
    OdooWorkbenchBillingAuthoringReader,
    OdooWorkbenchBillingFieldMapping,
)
from app.models.workbench_review_billing_evidence import WorkbenchReviewBillingEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.persistence import SqlAlchemyReviewBillingEvidenceReader, SqlAlchemyReviewRepository


def test_odoo_reader_uses_exact_parent_review_company_linkage_and_parent_id() -> None:
    adapter = RecordingAdapter(parent_records=[_parent_record()], billing_records=[_billing_record()])
    reader = OdooWorkbenchBillingAuthoringReader(adapter=adapter, mapping=_mapping())

    rows = reader.get_billing_authoring(review_id="review-1", company_id=7)

    assert adapter.calls[0] == {
        "method": "search_read",
        "model": "x_ipp_import_workbench",
        "domain": [["x_review_id", "=", "review-1"], ["x_company_id", "=", 7]],
        "fields": ["id", "x_review_id", "x_company_id", "x_version"],
        "limit": 2,
        "offset": 0,
    }
    assert adapter.calls[1]["method"] == "search_read_all"
    assert adapter.calls[1]["model"] == "x_ipp_billing_instruction"
    assert adapter.calls[1]["domain"] == [["x_import_review_id", "=", 42]]
    assert rows[0].billing_group_key == "BILL-A"


def test_odoo_reader_rejects_missing_or_ambiguous_parent() -> None:
    with pytest.raises(WorkbenchCandidateNotFoundError):
        OdooWorkbenchBillingAuthoringReader(
            adapter=RecordingAdapter(parent_records=[]),
            mapping=_mapping(),
        ).get_billing_authoring(review_id="review-1", company_id=7)
    with pytest.raises(WorkbenchCandidateAmbiguityError):
        OdooWorkbenchBillingAuthoringReader(
            adapter=RecordingAdapter(
                parent_records=[_parent_record(), _parent_record(id=43)],
            ),
            mapping=_mapping(),
        ).get_billing_authoring(review_id="review-1", company_id=7)


def test_odoo_reader_rejects_out_of_scope_returned_parent_or_child_link() -> None:
    with pytest.raises(WorkbenchCandidateDataError):
        OdooWorkbenchBillingAuthoringReader(
            adapter=RecordingAdapter(
                parent_records=[_parent_record(x_review_id="other-review")],
                billing_records=[_billing_record()],
            ),
            mapping=_mapping(),
        ).get_billing_authoring(review_id="review-1", company_id=7)
    with pytest.raises(WorkbenchCandidateDataError):
        OdooWorkbenchBillingAuthoringReader(
            adapter=RecordingAdapter(
                parent_records=[_parent_record()],
                billing_records=[_billing_record(x_import_review_id=[43, "other"])],
            ),
            mapping=_mapping(),
        ).get_billing_authoring(review_id="review-1", company_id=7)


def test_many2one_ids_and_many2many_tax_ids_are_parsed_by_id_and_display_names_ignored() -> None:
    row = OdooWorkbenchBillingAuthoringReader(
        adapter=RecordingAdapter(
            parent_records=[_parent_record()],
            billing_records=[
                _billing_record(
                    x_customer_id=[501, "Not Authority"],
                    x_product_id=[901, "Ignored Product"],
                    x_currency_id=[31, "Localized Turkish Lira Label"],
                    x_sales_tax_ids=[[1901, "VAT 20"], [1902, "VAT 10"]],
                )
            ],
        ),
        mapping=_mapping(),
    ).get_billing_authoring(review_id="review-1", company_id=7)[0]

    assert row.customer_id == 501
    assert row.product_id == 901
    assert row.currency_id == 31
    assert row.sales_tax_ids == (1901, 1902)


@pytest.mark.parametrize("bad_currency", [False, "TRY", ["TRY", 31], [0, "TRY"], [], None])
def test_malformed_currency_many2one_is_rejected(bad_currency: object) -> None:
    with pytest.raises(WorkbenchCandidateDataError):
        OdooWorkbenchBillingAuthoringReader(
            adapter=RecordingAdapter(
                parent_records=[_parent_record()],
                billing_records=[_billing_record(x_currency_id=bad_currency)],
            ),
            mapping=_mapping(),
        ).get_billing_authoring(review_id="review-1", company_id=7)


@pytest.mark.parametrize("quantity", ["1.500", 2, 2.5, Decimal("3.25")])
def test_decimal_inputs_are_accepted_without_business_float_arithmetic(quantity: object) -> None:
    row = OdooWorkbenchBillingAuthoringReader(
        adapter=RecordingAdapter(
            parent_records=[_parent_record()],
            billing_records=[_billing_record(x_quantity=quantity)],
        ),
        mapping=_mapping(),
    ).get_billing_authoring(review_id="review-1", company_id=7)[0]

    assert row.quantity == Decimal(str(quantity))


@pytest.mark.parametrize(
    "bad_value",
    [True, float("nan"), float("inf"), "NaN", "Infinity", "0", "-1", None],
)
def test_malformed_numeric_values_are_rejected(bad_value: object) -> None:
    with pytest.raises(WorkbenchCandidateDataError):
        OdooWorkbenchBillingAuthoringReader(
            adapter=RecordingAdapter(
                parent_records=[_parent_record()],
                billing_records=[_billing_record(x_quantity=bad_value)],
            ),
            mapping=_mapping(),
        ).get_billing_authoring(review_id="review-1", company_id=7)


@pytest.mark.parametrize(
    "field",
    [
        "x_billing_group_key",
        "x_allocation_key",
        "x_customer_id",
        "x_product_id",
        "x_quantity",
        "x_unit_price",
        "x_currency_id",
        "x_sales_tax_ids",
    ],
)
def test_missing_required_fields_are_rejected(field: str) -> None:
    record = _billing_record()
    record[field] = False

    with pytest.raises(WorkbenchCandidateDataError):
        OdooWorkbenchBillingAuthoringReader(
            adapter=RecordingAdapter(parent_records=[_parent_record()], billing_records=[record]),
            mapping=_mapping(),
        ).get_billing_authoring(review_id="review-1", company_id=7)


def test_reference_validator_reads_exact_authored_ids() -> None:
    row = _authoring_row()
    partner_repository = StaticReferenceRepository((PartnerReference(id=501),))
    product_repository = StaticReferenceRepository((ProductReference(id=901),))
    tax_repository = StaticReferenceRepository((SalesTaxReference(id=1901, usage_type="sale"),))
    currency_repository = StaticReferenceRepository((CurrencyReference(id=31, code="TRY"),))
    validator = WorkbenchBillingReferenceValidator(
        partner_repository=partner_repository,
        product_repository=product_repository,
        sales_tax_repository=tax_repository,
        currency_repository=currency_repository,
    )

    validator.validate_billing_authoring((row,), requested_company_id=7)

    assert partner_repository.calls == [(501,)]
    assert product_repository.calls == [(901,)]
    assert tax_repository.calls == [(1901,)]
    assert currency_repository.calls == [(31,)]


def test_reference_validator_rejects_wrong_company_inactive_and_purchase_tax() -> None:
    with pytest.raises(WorkbenchErpReferenceCompanyMismatchError):
        _reference_validator(partners=(PartnerReference(id=501, company_id=8),)).validate_billing_authoring(
            (_authoring_row(),),
            requested_company_id=7,
        )
    with pytest.raises(WorkbenchErpReferenceTypeError):
        _reference_validator(products=(ProductReference(id=901, active=False),)).validate_billing_authoring(
            (_authoring_row(),),
            requested_company_id=7,
        )
    with pytest.raises(WorkbenchErpReferenceTypeError):
        _reference_validator(taxes=(SalesTaxReference(id=1901, usage_type="purchase"),)).validate_billing_authoring(
            (_authoring_row(),),
            requested_company_id=7,
        )
    with pytest.raises(WorkbenchErpReferenceNotFoundError):
        _reference_validator(currencies=()).validate_billing_authoring((_authoring_row(),), requested_company_id=7)
    with pytest.raises(WorkbenchErpReferenceTypeError):
        _reference_validator(
            currencies=(CurrencyReference(id=31, code="TRY", active=False),),
        ).validate_billing_authoring(
            (_authoring_row(),),
            requested_company_id=7,
        )


def test_capture_use_case_persists_stage_one_and_groups_rows(session: Session) -> None:
    repository = _repository_with_review(session)
    use_case = _capture_use_case(
        repository,
        rows=(
            _authoring_row(odoo_record_id=2, allocation_key="ALLOC-B", billing_group_key="BILL-A", sequence=2),
            _authoring_row(odoo_record_id=1, allocation_key="ALLOC-A", billing_group_key="BILL-A", sequence=1),
            _authoring_row(
                odoo_record_id=3,
                allocation_key="ALLOC-C",
                billing_group_key="BILL-B",
                customer_id=502,
                sequence=1,
            ),
        ),
        candidate=_candidate(_allocation("ALLOC-A", 501), _allocation("ALLOC-B", 501), _allocation("ALLOC-C", 502)),
    )

    result = use_case.execute(CaptureOdooWorkbenchBillingEvidenceCommand(review_id="review-1", company_id=7))

    assert result.billing_keys == ("BILL-A", "BILL-B")
    instructions = SqlAlchemyReviewBillingEvidenceReader(session).get_billing_instructions(
        review_id="review-1",
        company_id=7,
        review_version=1,
    )
    assert tuple(instruction.billing_key for instruction in instructions) == ("BILL-A", "BILL-B")
    assert tuple(line.allocation_key for line in instructions[0].lines) == ("ALLOC-A", "ALLOC-B")
    assert instructions[0].lines[0].unit_price == Decimal("150.00")
    assert instructions[0].lines[0].product_id == 901
    assert instructions[0].lines[0].sales_tax_ids == (1901,)


def test_capture_uses_repository_validated_currency_code_not_odoo_display_label(session: Session) -> None:
    repository = _repository_with_review(session)
    rows = (
        _authoring_row(odoo_record_id=1, allocation_key="ALLOC-A", currency_id=31),
        _authoring_row(odoo_record_id=2, allocation_key="ALLOC-B", currency_id=31, sequence=2),
    )
    use_case = _capture_use_case(
        repository,
        rows=rows,
        candidate=_candidate(_allocation("ALLOC-A", 501), _allocation("ALLOC-B", 501)),
        currencies=(CurrencyReference(id=31, code="TRY"),),
    )

    use_case.execute(CaptureOdooWorkbenchBillingEvidenceCommand(review_id="review-1", company_id=7))

    instruction = SqlAlchemyReviewBillingEvidenceReader(session).get_billing_instructions(
        review_id="review-1",
        company_id=7,
        review_version=1,
    )[0]
    assert instruction.currency == "TRY"


def test_capture_rejects_group_with_different_currency_ids_even_if_display_text_matches(session: Session) -> None:
    repository = _repository_with_review(session)

    with pytest.raises(ReviewDataIntegrityError):
        _capture_use_case(
            repository,
            rows=(
                _authoring_row(odoo_record_id=1, allocation_key="ALLOC-A", currency_id=31),
                _authoring_row(odoo_record_id=2, allocation_key="ALLOC-B", currency_id=32, sequence=2),
            ),
            candidate=_candidate(_allocation("ALLOC-A", 501), _allocation("ALLOC-B", 501)),
            currencies=(CurrencyReference(id=31, code="TRY"), CurrencyReference(id=32, code="TRY")),
        ).execute(CaptureOdooWorkbenchBillingEvidenceCommand(review_id="review-1", company_id=7))

    assert session.query(WorkbenchReviewBillingEvidence).count() == 0


def test_capture_rejects_unready_stale_or_wrong_linkage(session: Session) -> None:
    repository = _repository_with_review(session)
    base_command = CaptureOdooWorkbenchBillingEvidenceCommand(review_id="review-1", company_id=7)
    cases = [
        {"rows": (_authoring_row(billing_ready=False),), "candidate": _candidate(_allocation("ALLOC-A", 501))},
        {"rows": (_authoring_row(review_version=2),), "candidate": _candidate(_allocation("ALLOC-A", 501))},
        {"rows": (_authoring_row(allocation_key="UNKNOWN"),), "candidate": _candidate(_allocation("ALLOC-A", 501))},
        {
            "rows": (_authoring_row(),),
            "candidate": _candidate(
                _allocation(
                    "ALLOC-A",
                    501,
                    allocation_type=BusinessContextAllocationType.SALES_ORDER_COST,
                )
            ),
        },
        {
            "rows": (_authoring_row(),),
            "candidate": _candidate(_allocation("ALLOC-A", 501, customer_invoice_id=9001)),
        },
        {"rows": (_authoring_row(customer_id=999),), "candidate": _candidate(_allocation("ALLOC-A", 501))},
        {
            "rows": (_authoring_row(), _authoring_row(odoo_record_id=2, billing_group_key="BILL-B")),
            "candidate": _candidate(_allocation("ALLOC-A", 501)),
        },
    ]

    for case in cases:
        with pytest.raises(ReviewDataIntegrityError):
            _capture_use_case(repository, rows=case["rows"], candidate=case["candidate"]).execute(base_command)
        assert session.query(WorkbenchReviewBillingEvidence).count() == 0


def test_capture_replay_idempotency_and_exact_set_conflicts(session: Session) -> None:
    repository = _repository_with_review(session)
    command = CaptureOdooWorkbenchBillingEvidenceCommand(review_id="review-1", company_id=7)
    rows = (_authoring_row(),)
    use_case = _capture_use_case(repository, rows=rows, candidate=_candidate(_allocation("ALLOC-A", 501)))

    first = use_case.execute(command)
    second = use_case.execute(command)

    assert second == first
    assert session.query(WorkbenchReviewBillingEvidence).count() == 1
    with pytest.raises(ReviewIdempotencyConflictError):
        _capture_use_case(
            repository,
            rows=(_authoring_row(unit_price=Decimal("151.00")),),
            candidate=_candidate(_allocation("ALLOC-A", 501)),
        ).execute(command)
    with pytest.raises(ReviewIdempotencyConflictError):
        _capture_use_case(
            repository,
            rows=(
                _authoring_row(),
                _authoring_row(odoo_record_id=2, allocation_key="ALLOC-B", billing_group_key="BILL-B"),
            ),
            candidate=_candidate(_allocation("ALLOC-A", 501), _allocation("ALLOC-B", 501)),
        ).execute(command)

    assert session.query(WorkbenchReviewBillingEvidence).count() == 1


def test_capture_does_not_use_cost_allocation_source_invoice_tax_or_pricelist_data(session: Session) -> None:
    repository = _repository_with_review(session)
    allocation = _allocation("ALLOC-A", 501, amount=Decimal("999.00"), percentage=Decimal("99.00"))
    use_case = _capture_use_case(
        repository,
        rows=(_authoring_row(unit_price=Decimal("150.00"), product_id=901, sales_tax_ids=(1901,)),),
        candidate=_candidate(allocation),
    )

    use_case.execute(CaptureOdooWorkbenchBillingEvidenceCommand(review_id="review-1", company_id=7))

    instruction = SqlAlchemyReviewBillingEvidenceReader(session).get_billing_instructions(
        review_id="review-1",
        company_id=7,
        review_version=1,
    )[0]
    assert instruction.lines[0].unit_price == Decimal("150.00")
    assert instruction.lines[0].product_id == 901
    assert instruction.lines[0].sales_tax_ids == (1901,)


def test_no_hub_ui_or_provider_logic_in_billing_capture_application_layer() -> None:
    source = inspect.getsource(capture_use_cases).lower()

    for forbidden in ("fastapi", "html", "react", "vue", "pricelist", "fuzzy", "openai", "uyumsoft"):
        assert forbidden not in source


class RecordingAdapter:
    def __init__(
        self,
        *,
        parent_records: list[dict[str, Any]],
        billing_records: list[dict[str, Any]] | None = None,
    ) -> None:
        self.parent_records = parent_records
        self.billing_records = billing_records or []
        self.calls: list[dict[str, Any]] = []

    def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        self.calls.append(
            {
                "method": "search_read",
                "model": model,
                "domain": domain,
                "fields": fields,
                "limit": limit,
                "offset": offset,
            }
        )
        return tuple(self.parent_records)

    def search_read_all(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        page_size: int | None = None,
        max_records: int | None = None,
    ) -> tuple[dict[str, Any], ...]:
        self.calls.append(
            {
                "method": "search_read_all",
                "model": model,
                "domain": domain,
                "fields": fields,
                "page_size": page_size,
                "max_records": max_records,
            }
        )
        return tuple(self.billing_records)


class StaticCandidateReader:
    def __init__(self, candidate: OdooWorkbenchDecisionCandidate) -> None:
        self.candidate = candidate

    def get_ready_decision(self, *, review_id: str, company_id: int) -> OdooWorkbenchDecisionCandidate:
        assert (review_id, company_id) == ("review-1", 7)
        return self.candidate

    def list_ready_decisions(self, *, company_id: int, limit: int) -> tuple[OdooWorkbenchDecisionCandidate, ...]:
        raise AssertionError("list_ready_decisions is not used for billing capture")


class StaticBillingReader:
    def __init__(self, rows: tuple[WorkbenchBillingAuthoringRow, ...]) -> None:
        self.rows = rows

    def get_billing_authoring(self, *, review_id: str, company_id: int) -> tuple[WorkbenchBillingAuthoringRow, ...]:
        assert (review_id, company_id) == ("review-1", 7)
        return self.rows


class StaticReferenceRepository:
    def __init__(self, records: tuple[object, ...]) -> None:
        self.records = records
        self.calls: list[tuple[object, ...]] = []

    def find_partners_by_ids(self, ids: tuple[int, ...]) -> tuple[PartnerReference, ...]:
        self.calls.append(ids)
        return tuple(record for record in self.records if isinstance(record, PartnerReference))

    def find_products_by_ids(self, ids: tuple[int, ...]) -> tuple[ProductReference, ...]:
        self.calls.append(ids)
        return tuple(record for record in self.records if isinstance(record, ProductReference))

    def find_sales_taxes_by_ids(self, ids: tuple[int, ...]) -> tuple[SalesTaxReference, ...]:
        self.calls.append(ids)
        return tuple(record for record in self.records if isinstance(record, SalesTaxReference))

    def find_currencies_by_ids(self, ids: tuple[int, ...]) -> tuple[CurrencyReference, ...]:
        self.calls.append(ids)
        return tuple(record for record in self.records if isinstance(record, CurrencyReference))

    def find_currencies_by_codes(self, codes: tuple[str, ...]) -> tuple[CurrencyReference, ...]:
        self.calls.append(codes)
        return tuple(record for record in self.records if isinstance(record, CurrencyReference))


def _reference_validator(
    *,
    partners: tuple[PartnerReference, ...] = (PartnerReference(id=501), PartnerReference(id=502)),
    products: tuple[ProductReference, ...] = (ProductReference(id=901),),
    taxes: tuple[SalesTaxReference, ...] = (SalesTaxReference(id=1901, usage_type="sale"),),
    currencies: tuple[CurrencyReference, ...] = (CurrencyReference(id=31, code="TRY"),),
) -> WorkbenchBillingReferenceValidator:
    validator = WorkbenchBillingReferenceValidator(
        partner_repository=StaticReferenceRepository(partners),
        product_repository=StaticReferenceRepository(products),
        sales_tax_repository=StaticReferenceRepository(taxes),
        currency_repository=StaticReferenceRepository(currencies),
    )
    return validator


def _capture_use_case(
    repository: SqlAlchemyReviewRepository,
    *,
    rows: tuple[WorkbenchBillingAuthoringRow, ...],
    candidate: OdooWorkbenchDecisionCandidate,
    currencies: tuple[CurrencyReference, ...] = (CurrencyReference(id=31, code="TRY"),),
) -> CaptureOdooWorkbenchBillingEvidenceUseCase:
    return CaptureOdooWorkbenchBillingEvidenceUseCase(
        review_reader=repository,
        candidate_reader=StaticCandidateReader(candidate),
        billing_authoring_reader=StaticBillingReader(rows),
        billing_evidence_writer=repository,
        reference_validator=_reference_validator(currencies=currencies),
    )


def _repository_with_review(session: Session) -> SqlAlchemyReviewRepository:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item(), company_id=7, idempotency_key="review-key-1")
    return repository


def _candidate(*allocations: BusinessContextAllocation, expected_version: int = 1) -> OdooWorkbenchDecisionCandidate:
    return OdooWorkbenchDecisionCandidate(
        odoo_record_id=42,
        review_id="review-1",
        company_id=7,
        expected_version=expected_version,
        decision=ReviewDecisionType.SELECT_WORKFLOW,
        idempotency_key="odoo-key-1",
        decided_by_odoo_user_id=11,
        decided_at=datetime(2026, 8, 11, 12, 0, tzinfo=UTC),
        decision_ready=True,
        selected_workflow=WorkflowType.VENDOR_BILL,
        business_context_allocations=BusinessContextAllocationSet(
            allocations=allocations,
            completeness=AllocationCompleteness.PARTIAL,
            invoice_total=Decimal("999.00"),
            currency="TRY",
        ),
    )


def _allocation(
    allocation_key: str,
    recharge_partner_id: int,
    *,
    allocation_type: BusinessContextAllocationType = BusinessContextAllocationType.CUSTOMER_RECHARGE,
    customer_invoice_id: int | None = None,
    amount: Decimal = Decimal("10.00"),
    percentage: Decimal | None = None,
    sales_order_id: int | None = None,
) -> BusinessContextAllocation:
    return BusinessContextAllocation(
        allocation_key=allocation_key,
        allocation_type=allocation_type,
        amount=amount,
        percentage=percentage,
        currency="TRY",
        recharge_partner_id=recharge_partner_id,
        customer_invoice_id=customer_invoice_id,
        sales_order_id=sales_order_id
        or (7001 if allocation_type is BusinessContextAllocationType.SALES_ORDER_COST else None),
    )


def _authoring_row(
    *,
    odoo_record_id: int = 1,
    review_version: int = 1,
    billing_group_key: str = "BILL-A",
    allocation_key: str = "ALLOC-A",
    customer_id: int = 501,
    product_id: int = 901,
    currency_id: int = 31,
    quantity: Decimal = Decimal("1.000"),
    unit_price: Decimal = Decimal("150.00"),
    sales_tax_ids: tuple[int, ...] = (1901,),
    billing_ready: bool = True,
    sequence: int = 1,
) -> WorkbenchBillingAuthoringRow:
    return WorkbenchBillingAuthoringRow(
        odoo_record_id=odoo_record_id,
        review_id="review-1",
        company_id=7,
        review_version=review_version,
        billing_group_key=billing_group_key,
        allocation_key=allocation_key,
        customer_id=customer_id,
        product_id=product_id,
        description=f"Recharge {allocation_key}",
        quantity=quantity,
        unit_price=unit_price,
        currency_id=currency_id,
        sales_tax_ids=sales_tax_ids,
        billing_ready=billing_ready,
        sequence=sequence,
    )


def _review_item() -> ReviewItem:
    return ReviewItem(
        review_id="review-1",
        invoice_id="INV-1",
        invoice_number="INV-1",
        supplier_tax_number="1234567890",
        supplier_name="Supplier",
        invoice_date=date(2026, 8, 11),
        currency="TRY",
        total_amount=Decimal("999.00"),
        workflow=WorkflowType.VENDOR_BILL,
        status=ReviewStatus.PENDING_REVIEW,
        review_reasons=(
            ManualReviewReason(
                code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
                message="Review required.",
                source="product_matching",
            ),
        ),
        version=1,
    )


def _parent_record(**overrides: object) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": 42,
        "x_review_id": "review-1",
        "x_company_id": [7, "ICT"],
        "x_version": 1,
    }
    record.update(overrides)
    return record


def _billing_record(**overrides: object) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": 1,
        "x_import_review_id": [42, "review-1"],
        "x_billing_group_key": "BILL-A",
        "x_allocation_key": "ALLOC-A",
        "x_customer_id": [501, "Customer"],
        "x_product_id": [901, "Product"],
        "x_description": "Recharge ALLOC-A",
        "x_quantity": "1.000",
        "x_unit_price": "150.00",
        "x_currency_id": [1, "TRY"],
        "x_sales_tax_ids": [1901],
        "x_billing_ready": True,
        "x_sequence": 1,
    }
    record.update(overrides)
    return record


def _mapping() -> OdooWorkbenchBillingFieldMapping:
    return OdooWorkbenchBillingFieldMapping(
        parent_model="x_ipp_import_workbench",
        parent_review_id="x_review_id",
        parent_company_id="x_company_id",
        parent_review_version="x_version",
        billing_model="x_ipp_billing_instruction",
        parent_many2one_field="x_import_review_id",
        billing_group_key="x_billing_group_key",
        allocation_key="x_allocation_key",
        customer_id="x_customer_id",
        product_id="x_product_id",
        description="x_description",
        quantity="x_quantity",
        unit_price="x_unit_price",
        currency_id="x_currency_id",
        sales_tax_ids="x_sales_tax_ids",
        billing_ready="x_billing_ready",
        sequence="x_sequence",
    )


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[WorkbenchReviewItem.__table__, WorkbenchReviewBillingEvidence.__table__])
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session
