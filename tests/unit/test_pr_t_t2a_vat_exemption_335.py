"""PR T T2-A: UBL-TR VAT exemption code 335 -> approved Odoo purchase tax.

Every document, party, number and tax id here is synthetic. Canonical invoices are always
parsed from the same bytes the source-tax facts are extracted from, exactly as import does.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.application.commands import ImportInvoiceCommand
from app.application.execution import ExecutionMode
from app.application.execution.contracts import ExecutionStepStatus
from app.application.execution.vendor_bill_strategy import (
    SOURCE_TAX_BLOCKED_ERROR_CODE,
    SOURCE_TAX_UNAVAILABLE_ERROR_CODE,
    VendorBillExecutionStrategy,
)
from app.application.rules.deterministic import DIRECT_VENDOR_BILL_RULE_ID, DeterministicRuleEngine
from app.application.source_tax import (
    SourceDocumentIssue,
    SourceTaxAssessment,
    SourceTaxFactsUnavailableError,
    SourceTaxSafetyBlockedError,
)
from app.application.source_tax.exemption import DISABLED_VAT_EXEMPTION_POLICY, VatExemptionTaxPolicy
from app.application.source_tax.reader import SourceTaxSafetyCheck
from app.application.workbench.decision_use_cases import SubmitReviewDecisionUseCase
from app.application.workflow import ManualReviewReasonCode, WorkflowType
from app.composition.source_tax import build_vat_exemption_tax_policy
from app.core.config import Settings
from app.domain.invoice import InternalInvoice
from app.domain.invoice.parser import parse_ubl_invoice
from app.domain.invoice.tax_facts import extract_source_tax_facts
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.tax_repository import OdooExemptionTaxReader
from app.matching import PartnerMatchStatus
from app.persistence.execution_source_invoice_reader import _tax_match_from_data, _tax_match_to_data
from app.tax_mapping import TaxCandidate, TaxMappingEngine, TaxMatchStatus, TaxType
from app.tax_mapping.exemption import (
    EXEMPTION_TAX_FIELDS,
    VatExemptionConfigError,
    assess_invoice_exemption,
    exemption_match_result,
    exemption_tax_problems,
    validate_vat_exemption_tax_config,
)
from app.tax_mapping.result import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult
from tests.unit.resale_execution_support import NON_RESALE_ACCOUNTING_CHECK, TWO_DECIMAL_CURRENCY_READER
from tests.unit.source_tax_support import FixedSourceTaxFactsReader
from tests.unit.test_operating_expense_classification import _FakeMatcher, _matched_product_match, _partner_match
from tests.unit.test_review_decision_use_case import (
    RecordingDecisionWriter,
    RecordingEvidenceReader,
    _FakeUnitOfWork,
    _select_workflow_command,
    _source_evidence,
)
from tests.unit.test_vendor_bill_execution_strategy import (
    RecordingSourceInvoiceReader,
    RecordingVendorBillBuilder,
    RecordingVendorBillWriter,
    _source,
    _step_request,
)

COMPANY_ID = 7
TAX_ID = 9335
OTHER_TAX_ID = 9336
GENERIC_ZERO_TAX_ID = 6
UUID = "7e570000-0000-4000-8000-0000000003a5"
UNSUPPORTED = ManualReviewReasonCode.UNSUPPORTED_INVOICE_CONTENT


# ===================================================================== synthetic documents


@dataclass(frozen=True, slots=True)
class Sub:
    type_code: str | None = "0015"
    name: str = "KDV"
    percent: str | None = None
    amount: str = "0"
    taxable: str = "419.49"
    exemption_code: str | None = "335"
    exemption_reason: str | None = "Muafiyet uygulanmıştır"


def _subtotal(sub: Sub) -> str:
    percent = f"<cbc:Percent>{sub.percent}</cbc:Percent>" if sub.percent is not None else ""
    code = (
        f"<cbc:TaxExemptionReasonCode>{sub.exemption_code}</cbc:TaxExemptionReasonCode>" if sub.exemption_code else ""
    )
    reason = f"<cbc:TaxExemptionReason>{sub.exemption_reason}</cbc:TaxExemptionReason>" if sub.exemption_reason else ""
    type_code = f"<cbc:TaxTypeCode>{sub.type_code}</cbc:TaxTypeCode>" if sub.type_code else ""
    return f"""
      <cac:TaxSubtotal>
        <cbc:TaxableAmount currencyID="TRY">{sub.taxable}</cbc:TaxableAmount>
        <cbc:TaxAmount currencyID="TRY">{sub.amount}</cbc:TaxAmount>
        {percent}
        <cac:TaxCategory>{code}{reason}
          <cac:TaxScheme><cbc:Name>{sub.name}</cbc:Name>{type_code}</cac:TaxScheme>
        </cac:TaxCategory>
      </cac:TaxSubtotal>"""


WITHHOLDING_XML = """
    <cac:WithholdingTaxTotal>
      <cbc:TaxAmount currencyID="TRY">10.00</cbc:TaxAmount>
      <cac:TaxSubtotal><cbc:TaxableAmount currencyID="TRY">20.00</cbc:TaxableAmount>
        <cbc:TaxAmount currencyID="TRY">10.00</cbc:TaxAmount><cbc:Percent>50</cbc:Percent>
        <cac:TaxCategory><cac:TaxScheme><cbc:Name>KDV TEVKIFAT</cbc:Name>
          <cbc:TaxTypeCode>650</cbc:TaxTypeCode></cac:TaxScheme></cac:TaxCategory>
      </cac:TaxSubtotal>
    </cac:WithholdingTaxTotal>"""


def document(
    *lines: tuple[Sub, ...],
    invoice_type: str = "ISTISNA",
    invoice_subtotals: tuple[Sub, ...] | None = None,
    line_ids: tuple[str, ...] | None = None,
    line_withholding: bool = False,
) -> bytes:
    lines = lines or ((Sub(),),)
    ids = line_ids or tuple(str(index) for index in range(1, len(lines) + 1))
    invoice_level = invoice_subtotals if invoice_subtotals is not None else tuple(sub for line in lines for sub in line)
    lines_xml = "".join(
        f"""
  <cac:InvoiceLine>
    <cbc:ID>{line_id}</cbc:ID>
    <cbc:InvoicedQuantity unitCode="C62">1</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="TRY">419.49</cbc:LineExtensionAmount>
    <cac:TaxTotal><cbc:TaxAmount currencyID="TRY">0</cbc:TaxAmount>{"".join(_subtotal(sub) for sub in subs)}
    </cac:TaxTotal>{WITHHOLDING_XML if line_withholding else ""}
    <cac:Item><cbc:Name>Synthetic printed book</cbc:Name></cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="TRY">419.49</cbc:PriceAmount></cac:Price>
  </cac:InvoiceLine>"""
        for line_id, subs in zip(ids, lines, strict=True)
    )
    invoice_tax_xml = (
        f"""<cac:TaxTotal><cbc:TaxAmount currencyID="TRY">0</cbc:TaxAmount>
    {"".join(_subtotal(sub) for sub in invoice_level)}</cac:TaxTotal>"""
        if invoice_level
        else ""
    )
    return f"""<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ProfileID>TICARIFATURA</cbc:ProfileID>
  <cbc:ID>SYN2026T20000001</cbc:ID>
  <cbc:UUID>{UUID}</cbc:UUID>
  <cbc:IssueDate>2026-10-01</cbc:IssueDate>
  <cbc:InvoiceTypeCode>{invoice_type}</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>TRY</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyIdentification><cbc:ID schemeID="VKN">1000000146</cbc:ID></cac:PartyIdentification>
    <cac:PartyName><cbc:Name>SYNTHETIC BOOK SUPPLIER</cbc:Name></cac:PartyName>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party>
    <cac:PartyIdentification><cbc:ID schemeID="VKN">2000000010</cbc:ID></cac:PartyIdentification>
    <cac:PartyName><cbc:Name>SYNTHETIC BUYER</cbc:Name></cac:PartyName>
  </cac:Party></cac:AccountingCustomerParty>
  {invoice_tax_xml}
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="TRY">419.49</cbc:LineExtensionAmount>
    <cbc:PayableAmount currencyID="TRY">419.49</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  {lines_xml}
</Invoice>""".encode()


VAT_20 = Sub(percent="20", amount="83.90", exemption_code=None, exemption_reason=None)


def invoice_and_assessment(content: bytes) -> tuple[InternalInvoice, SourceTaxAssessment]:
    return parse_ubl_invoice(content), SourceTaxAssessment.verified(extract_source_tax_facts(content))


# ===================================================================== fake Odoo tax


def approved_tax_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "id": TAX_ID,
        "name": "KDV İstisna — 335 — Basılı Kitap",
        "active": True,
        "company_id": [COMPANY_ID, "Synthetic Company"],
        "country_code": "TR",
        "type_tax_use": "purchase",
        "amount_type": "percent",
        "amount": 0.0,
        "children_tax_ids": [],
        "price_include": False,
        "price_include_override": False,
        "include_base_amount": False,
        "tax_exigibility": "on_invoice",
        "has_negative_factor": False,
        "fiscal_position_ids": [],
        "original_tax_ids": [],
        "replacing_tax_ids": [],
    }
    record.update(overrides)
    return record


class FakeTaxReader:
    def __init__(self, record: dict[str, object] | None = None, *, error: Exception | None = None) -> None:
        self.record = record if record is not None else approved_tax_record()
        self.error = error
        self.calls: list[int] = []
        self.missing = False

    def read_exemption_tax(self, tax_id: int):
        self.calls.append(tax_id)
        if self.error is not None:
            raise self.error
        if self.missing:
            return None
        return dict(self.record) if self.record.get("id") == tax_id else None


def enabled_policy(reader: FakeTaxReader | None = None, *, tax_id: int = TAX_ID) -> VatExemptionTaxPolicy:
    return VatExemptionTaxPolicy(tax_ids_by_code={"335": tax_id}, tax_reader=reader or FakeTaxReader())


# ===================================================================== T1 tax mapping (real engine)


class FakeTaxRepository:
    """T1's Odoo tax candidates: 1% / 10% / 20% resolve uniquely, like production."""

    def __init__(self) -> None:
        self.calls: list[Decimal] = []

    def find_candidates(self, *, company_id, rate, tax_type):
        self.calls.append(rate)
        ids = {Decimal("1"): 36, Decimal("10"): 35, Decimal("20"): 34}
        tax_id = ids.get(rate)
        if tax_id is None:
            return ()
        return (
            TaxCandidate(
                tax_id=tax_id,
                company_id=company_id,
                tax_type=TaxType.VAT,
                rate=rate,
                usage_type="purchase",
                amount_type="percent",
                has_children=False,
            ),
        )


def engine(policy: VatExemptionTaxPolicy, invoice: InternalInvoice, repository: FakeTaxRepository | None = None):
    return DeterministicRuleEngine(
        partner_matcher=_FakeMatcher(_partner_match()),
        product_matcher=_FakeMatcher(_matched_product_match(invoice)),
        tax_mapper=TaxMappingEngine(repository or FakeTaxRepository()),
        exemption_tax_policy=policy,
    )


def command(invoice: InternalInvoice, assessment: SourceTaxAssessment | None, *, dry_run: bool = False):
    return ImportInvoiceCommand(
        invoice=invoice,
        idempotency_key="ettn:T2A",
        company_id=COMPANY_ID,
        dry_run=dry_run,
        source_tax_assessment=assessment,
    )


def statuses(result) -> tuple[TaxMatchStatus, ...]:
    return tuple(line.result.status for line in result.tax_match.line_results)


def reason_codes(result) -> tuple[ManualReviewReasonCode, ...]:
    manual = result.workflow_decision.manual_review
    return tuple(reason.code for reason in manual.reasons) if manual is not None else ()


# ===================================================================== 1-15: eligibility policy


def eligibility(content: bytes):
    invoice, assessment = invoice_and_assessment(content)
    return assess_invoice_exemption(invoice, assessment.facts)


def test_01_valid_335_without_percent_is_eligible() -> None:
    result = eligibility(document())

    assert result.eligible and result.code == "335" and result.problems == ()


def test_02_valid_335_with_percent_zero_is_eligible() -> None:
    assert eligibility(document((Sub(percent="0"),))).eligible
    assert eligibility(document((Sub(percent="0.00"),))).eligible


def test_valid_335_with_code_only_and_no_reason_text_is_eligible() -> None:
    assert eligibility(document((Sub(exemption_reason=None),))).eligible


def test_valid_335_on_every_line_of_a_multi_line_invoice_is_eligible() -> None:
    assert eligibility(document((Sub(),), (Sub(taxable="10.00"),))).eligible


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("03-nonzero-tax-amount", document((Sub(amount="0.01"),))),
        ("04-percent-above-zero", document((Sub(percent="1"),))),
        ("04-percent-empty", document((Sub(percent=""),))),
        ("05-satis", document(invoice_type="SATIS")),
        ("05-tevkifat", document(invoice_type="TEVKIFAT")),
        ("06-wrong-tax-type-code", document((Sub(type_code="9015"),))),
        ("06-missing-tax-type-code", document((Sub(type_code=None),))),
        ("07-missing-exemption-code", document((Sub(exemption_code=None, exemption_reason=None),))),
        ("08-reason-text-without-code", document((Sub(exemption_code=None),))),
        ("09-code-209", document((Sub(exemption_code="209"),))),
        ("10-code-351", document((Sub(exemption_code="351"),))),
        ("10-code-3350", document((Sub(exemption_code="3350"),))),
        ("11-mixed-335-and-vat-lines", document((Sub(),), (VAT_20,))),
        ("11-mixed-335-and-vat-on-one-line", document((Sub(), VAT_20))),
        ("11-two-335-subtotals-on-one-line", document((Sub(), Sub(taxable="1.00")))),
        ("13-invoice-level-base-mismatch", document(invoice_subtotals=(Sub(taxable="400.00"),))),
        ("13-invoice-level-code-209", document(invoice_subtotals=(Sub(exemption_code="209"),))),
        ("13-invoice-level-nonzero-tax", document(invoice_subtotals=(Sub(amount="1.00"),))),
        ("13-invoice-level-percent", document(invoice_subtotals=(Sub(percent="20"),))),
        ("14-line-withholding", document(line_withholding=True)),
        ("15-return-invoice", document(invoice_type="IADE")),
        ("negative-taxable-amount", document((Sub(taxable="-1.00"),))),
        ("non-vat-scheme-name", document((Sub(name="ÖTV"),))),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_ineligible_shapes(name: str, content: bytes) -> None:
    result = eligibility(content)

    assert not result.eligible, name
    assert result.code is None
    assert result.problems


def test_invoice_level_subtotals_are_optional() -> None:
    assert eligibility(document(invoice_subtotals=())).eligible


def test_12_canonical_evidence_from_another_document_never_aligns() -> None:
    facts = extract_source_tax_facts(document())
    for other in (
        document((Sub(),), (Sub(),)),  # extra line
        document(line_ids=("2",)),  # renumbered line
        document((Sub(taxable="419.48"),)),  # different base
        document((Sub(exemption_reason="Other reason"),)),  # different reason text
    ):
        assert not assess_invoice_exemption(parse_ubl_invoice(other), facts).eligible


def test_12_reordered_tax_subtotals_never_align() -> None:
    facts = extract_source_tax_facts(document((Sub(), Sub(taxable="1.00"))))
    invoice = parse_ubl_invoice(document((Sub(taxable="1.00"), Sub())))

    assert not assess_invoice_exemption(invoice, facts).eligible


def test_12_duplicate_or_missing_line_ids_are_refused() -> None:
    assert not eligibility(document((Sub(),), (Sub(),), line_ids=("1", "1"))).eligible
    assert not eligibility(document(line_ids=("",))).eligible


def test_12_facts_without_extracted_structure_are_refused() -> None:
    invoice, assessment = invoice_and_assessment(document())
    legacy = replace(assessment.facts, line_ids=None, line_tax_subtotals=(), invoice_tax_subtotals=())

    assert not assess_invoice_exemption(invoice, legacy).eligible


def test_malformed_source_amount_is_refused_without_invalidating_the_document() -> None:
    content = document(invoice_subtotals=(Sub(taxable="4l9.49"),))
    facts = extract_source_tax_facts(content)

    assert facts.invoice_tax_subtotals[0].malformed
    assert not assess_invoice_exemption(parse_ubl_invoice(content), facts).eligible


def test_facts_mirror_the_canonical_parser_positions() -> None:
    content = document((Sub(), Sub(taxable="1.00")), (Sub(percent="0"),))
    facts = extract_source_tax_facts(content)
    invoice = parse_ubl_invoice(content)

    assert facts.line_ids == ("1", "2")
    assert [(fact.line_id, fact.tax_index) for fact in facts.line_tax_subtotals] == [
        (line.line_number, index) for line in invoice.lines for index, _ in enumerate(line.taxes)
    ]
    first = facts.line_tax_subtotals[0]
    assert (first.tax_type_code, first.exemption_reason_code, first.exemption_reason) == (
        "0015",
        "335",
        "Muafiyet uygulanmıştır",
    )
    assert (first.percent_present, first.percent, first.tax_amount, first.taxable_amount) == (
        False,
        None,
        Decimal("0"),
        Decimal("419.49"),
    )
    assert facts.line_tax_subtotals[2].percent_present and facts.line_tax_subtotals[2].percent == Decimal("0")
    assert [fact.line_id for fact in facts.invoice_tax_subtotals] == [None, None, None]


def test_new_facts_leave_w1_verdicts_unchanged() -> None:
    facts = extract_source_tax_facts(document())

    assert facts.invoice_type_code == "ISTISNA"
    assert facts.tax_type_codes == ("0015",)
    assert not facts.has_withholding and not facts.has_unsupported_tax_content


# ===================================================================== rule engine (classification)


def test_valid_335_invoice_maps_every_line_to_the_approved_tax() -> None:
    invoice, assessment = invoice_and_assessment(document((Sub(),), (Sub(percent="0", taxable="10.00"),)))
    reader = FakeTaxReader()

    result = engine(enabled_policy(reader), invoice).evaluate(command(invoice, assessment))

    assert result.workflow is WorkflowType.VENDOR_BILL
    assert result.matched_rule == DIRECT_VENDOR_BILL_RULE_ID
    expected = exemption_match_result(code="335", tax_id=TAX_ID, company_id=COMPANY_ID)
    assert [line.result for line in result.tax_match.line_results] == [expected, expected]
    assert expected.status is TaxMatchStatus.MATCHED
    assert expected.tax_type is TaxType.EXEMPTION
    assert expected.tax_rate == Decimal("0")
    assert expected.matched_by == "source_exemption_code:335"
    assert expected.candidate_count == 1
    assert reader.calls == [TAX_ID]


def test_the_335_match_still_keeps_supplier_and_product_reasons() -> None:
    invoice, assessment = invoice_and_assessment(document())
    rule_engine = DeterministicRuleEngine(
        partner_matcher=_FakeMatcher(_partner_match(PartnerMatchStatus.NOT_FOUND)),
        product_matcher=_FakeMatcher(_matched_product_match(invoice)),
        tax_mapper=TaxMappingEngine(FakeTaxRepository()),
        exemption_tax_policy=enabled_policy(),
    )

    result = rule_engine.evaluate(command(invoice, assessment))

    assert result.workflow is WorkflowType.MANUAL_REVIEW
    # Like the reference invoice: the tax blocker is gone, the supplier (and, for an
    # identifier-free line, the operating-expense) reasons remain.
    assert ManualReviewReasonCode.SUPPLIER_NOT_FOUND in reason_codes(result)
    assert UNSUPPORTED not in reason_codes(result)
    assert statuses(result) == (TaxMatchStatus.MATCHED,)


@pytest.mark.parametrize(
    "content",
    [
        document((Sub(exemption_code="209"),)),
        document((Sub(exemption_code="351"),)),
        document((Sub(),), (VAT_20,)),
        document((Sub(amount="0.01"),)),
        document(invoice_type="SATIS"),
    ],
    ids=["209", "351", "mixed", "nonzero", "satis"],
)
def test_09_10_11_31_ineligible_invoices_keep_the_t1_result_without_reading_odoo(content: bytes) -> None:
    invoice, assessment = invoice_and_assessment(content)
    reader = FakeTaxReader()

    enabled = engine(enabled_policy(reader), invoice).evaluate(command(invoice, assessment))
    t1 = engine(DISABLED_VAT_EXEMPTION_POLICY, invoice).evaluate(command(invoice, assessment))

    assert enabled == t1
    assert reader.calls == []
    assert TaxMatchStatus.INVALID_INPUT in statuses(enabled)
    assert all(line.result.tax_type is not TaxType.EXEMPTION for line in enabled.tax_match.line_results)


def test_15_return_invoice_keeps_only_the_return_reason() -> None:
    invoice, assessment = invoice_and_assessment(document(invoice_type="IADE"))
    reader = FakeTaxReader()

    result = engine(enabled_policy(reader), invoice).evaluate(command(invoice, assessment))

    assert reason_codes(result) == (ManualReviewReasonCode.RETURN_INVOICE_NOT_SUPPORTED,)
    assert reader.calls == []


def test_14_withholding_invoice_keeps_the_withholding_block() -> None:
    invoice, assessment = invoice_and_assessment(document(line_withholding=True))
    reader = FakeTaxReader()

    result = engine(enabled_policy(reader), invoice).evaluate(command(invoice, assessment))

    assert result.workflow is WorkflowType.MANUAL_REVIEW
    assert reason_codes(result)[0] is ManualReviewReasonCode.WITHHOLDING_TAX_NOT_SUPPORTED
    assert reader.calls == []


def test_16_unverified_document_is_never_mapped() -> None:
    invoice, _ = invoice_and_assessment(document())
    reader = FakeTaxReader()
    unverified = SourceTaxAssessment.unverified(SourceDocumentIssue.HASH_MISMATCH)

    result = engine(enabled_policy(reader), invoice).evaluate(command(invoice, unverified))

    assert reason_codes(result)[0] is ManualReviewReasonCode.SOURCE_DOCUMENT_UNVERIFIED
    assert statuses(result) == (TaxMatchStatus.INVALID_INPUT,)
    assert reader.calls == []


def test_17_missing_assessment_is_never_mapped() -> None:
    invoice, _ = invoice_and_assessment(document())
    reader = FakeTaxReader()

    real = engine(enabled_policy(reader), invoice).evaluate(command(invoice, None))
    dry = engine(enabled_policy(reader), invoice).evaluate(command(invoice, None, dry_run=True))

    assert reason_codes(real)[0] is ManualReviewReasonCode.SOURCE_DOCUMENT_UNVERIFIED
    assert statuses(real) == statuses(dry) == (TaxMatchStatus.INVALID_INPUT,)
    assert reader.calls == []


def test_18_empty_configuration_reproduces_t1_exactly() -> None:
    invoice, assessment = invoice_and_assessment(document())

    result = engine(DISABLED_VAT_EXEMPTION_POLICY, invoice).evaluate(command(invoice, assessment))
    default = DeterministicRuleEngine(
        partner_matcher=_FakeMatcher(_partner_match()),
        product_matcher=_FakeMatcher(_matched_product_match(invoice)),
        tax_mapper=TaxMappingEngine(FakeTaxRepository()),
    ).evaluate(command(invoice, assessment))

    assert result == default
    assert result.workflow is WorkflowType.MANUAL_REVIEW
    assert reason_codes(result) == (UNSUPPORTED,)
    assert statuses(result) == (TaxMatchStatus.INVALID_INPUT,)
    assert DISABLED_VAT_EXEMPTION_POLICY.enabled is False


def test_30_ordinary_vat_rates_are_unchanged_with_the_policy_enabled() -> None:
    rows = (VAT_20, replace(VAT_20, percent="10", amount="41.95"), replace(VAT_20, percent="1", amount="4.19"))
    content = document(*((row,) for row in rows), invoice_type="SATIS")
    invoice, assessment = invoice_and_assessment(content)
    reader = FakeTaxReader()

    enabled = engine(enabled_policy(reader), invoice).evaluate(command(invoice, assessment))
    t1 = engine(DISABLED_VAT_EXEMPTION_POLICY, invoice).evaluate(command(invoice, assessment))

    assert enabled == t1
    assert [line.result.tax_id for line in enabled.tax_match.line_results] == [34, 35, 36]
    assert {line.result.matched_by for line in enabled.tax_match.line_results} == {"company_type_rate"}
    assert enabled.workflow is WorkflowType.VENDOR_BILL
    assert reader.calls == []


def test_invalid_configured_tax_keeps_the_invoice_blocked_with_an_explicit_reason() -> None:
    invoice, assessment = invoice_and_assessment(document())
    reader = FakeTaxReader(approved_tax_record(price_include=True))

    result = engine(enabled_policy(reader), invoice).evaluate(command(invoice, assessment))

    assert result.workflow is WorkflowType.MANUAL_REVIEW
    assert statuses(result) == (TaxMatchStatus.INVALID_INPUT,)
    assert "configured Odoo tax failed validation" in result.tax_match.line_results[0].result.reason
    assert reason_codes(result) == (UNSUPPORTED,)


def test_odoo_read_failure_during_classification_is_raised_not_swallowed() -> None:
    from app.application.rules.deterministic import TaxRuleEvaluationError

    invoice, assessment = invoice_and_assessment(document())
    reader = FakeTaxReader(error=ErpRepositoryError("Odoo unavailable."))

    with pytest.raises(TaxRuleEvaluationError):
        engine(enabled_policy(reader), invoice).evaluate(command(invoice, assessment))


def test_classification_is_deterministic() -> None:
    invoice, assessment = invoice_and_assessment(document())
    rule_engine = engine(enabled_policy(), invoice)

    assert rule_engine.evaluate(command(invoice, assessment)) == rule_engine.evaluate(command(invoice, assessment))


def test_cross_company_classification_uses_the_command_company() -> None:
    invoice, assessment = invoice_and_assessment(document())

    result = engine(enabled_policy(), invoice).evaluate(replace(command(invoice, assessment), company_id=8))

    assert statuses(result) == (TaxMatchStatus.INVALID_INPUT,)  # the tax belongs to company 7


# ===================================================================== 19-24: Odoo tax validation


def problems(record: dict[str, object] | None, *, tax_id: int = TAX_ID, company_id: int | None = COMPANY_ID):
    return exemption_tax_problems(record, expected_tax_id=tax_id, company_id=company_id, code="335")


def test_approved_tax_passes_every_check() -> None:
    assert problems(approved_tax_record()) == ()
    assert problems(approved_tax_record(price_include_override="tax_excluded")) == ()


def test_19_generic_zero_tax_is_rejected_whatever_its_record_says() -> None:
    assert problems(approved_tax_record(id=GENERIC_ZERO_TAX_ID), tax_id=GENERIC_ZERO_TAX_ID)
    assert problems(approved_tax_record(name="0%")) == ("Configured exemption tax name must carry the 335 token.",)


@pytest.mark.parametrize(
    ("overrides", "company_id"),
    [
        ({"active": False}, COMPANY_ID),  # 20 inactive
        ({"company_id": [8, "Other"]}, COMPANY_ID),  # 20 wrong company
        ({}, 8),  # 20 wrong company (caller side)
        ({}, None),
        ({"company_id": False}, COMPANY_ID),
        ({"country_code": "DE"}, COMPANY_ID),
        ({"type_tax_use": "sale"}, COMPANY_ID),
        ({"amount_type": "group"}, COMPANY_ID),  # 21 group tax
        ({"children_tax_ids": [17, 34]}, COMPANY_ID),  # 21 children
        ({"amount": 1.0}, COMPANY_ID),  # 22 nonzero amount
        ({"amount": "0"}, COMPANY_ID),
        ({"amount": False}, COMPANY_ID),
        ({"price_include": True}, COMPANY_ID),  # 23
        ({"price_include_override": "tax_included"}, COMPANY_ID),  # 23
        ({"include_base_amount": True}, COMPANY_ID),
        ({"tax_exigibility": "on_payment"}, COMPANY_ID),
        ({"has_negative_factor": True}, COMPANY_ID),
        ({"fiscal_position_ids": [3]}, COMPANY_ID),
        ({"original_tax_ids": [34]}, COMPANY_ID),
        ({"replacing_tax_ids": [40]}, COMPANY_ID),
        ({"name": "KDV İstisna — Basılı Kitap"}, COMPANY_ID),  # 24 no token
        ({"name": "KDV İstisna 3350"}, COMPANY_ID),  # 24 embedded token
        ({"name": "KDV İstisna A335"}, COMPANY_ID),
        ({"name": False}, COMPANY_ID),
        ({"id": OTHER_TAX_ID}, COMPANY_ID),
    ],
)
def test_20_to_24_invalid_tax_configurations(overrides: dict[str, object], company_id: int | None) -> None:
    assert problems(approved_tax_record(**overrides), company_id=company_id)


def test_20_missing_tax_and_missing_fields_are_rejected() -> None:
    assert problems(None) == ("Configured exemption tax was not found in the ERP.",)
    for field in EXEMPTION_TAX_FIELDS:
        record = approved_tax_record()
        del record[field]
        assert problems(record), field


def test_odoo_reader_reads_the_exact_id_including_archived_records() -> None:
    class Adapter:
        def __init__(self, records):
            self.records = records
            self.calls = []

        def search_read(self, **kwargs):
            self.calls.append(kwargs)
            return self.records

    adapter = Adapter((approved_tax_record(),))
    assert OdooExemptionTaxReader(adapter=adapter).read_exemption_tax(TAX_ID) == approved_tax_record()
    assert adapter.calls == [
        {
            "model": "account.tax",
            "domain": [["id", "=", TAX_ID], ["active", "in", [True, False]]],
            "fields": list(EXEMPTION_TAX_FIELDS),
            "limit": 2,
        }
    ]
    assert OdooExemptionTaxReader(adapter=Adapter(())).read_exemption_tax(TAX_ID) is None
    with pytest.raises(ErpRepositoryError):
        OdooExemptionTaxReader(adapter=Adapter((approved_tax_record(), approved_tax_record()))).read_exemption_tax(
            TAX_ID
        )
    with pytest.raises(ErpRepositoryError):
        OdooExemptionTaxReader(adapter=Adapter(())).read_exemption_tax(0)


# ===================================================================== 25: configuration


@pytest.mark.parametrize(
    "value",
    [
        {"209": 9209},
        {"351": 9351},
        {"335": 9335, "209": 9209},
        {" 335": 9335},
        {"335 ": 9335},
        {335: 9335},
        {"335": 0},
        {"335": -1},
        {"335": True},
        {"335": "9335"},
        {"335": 9335.0},
        {"335": GENERIC_ZERO_TAX_ID},
        ["335"],
        "335",
    ],
)
def test_25_invalid_configuration_is_rejected(value: object) -> None:
    with pytest.raises(VatExemptionConfigError):
        validate_vat_exemption_tax_config(value)


def test_25_duplicate_tax_ids_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.tax_mapping.exemption.APPROVED_VAT_EXEMPTION_CODES", frozenset({"335", "999"}))

    with pytest.raises(VatExemptionConfigError, match="dedicated"):
        validate_vat_exemption_tax_config({"335": TAX_ID, "999": TAX_ID})


def test_25_settings_default_is_empty_and_env_json_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAT_EXEMPTION_PURCHASE_TAX_IDS", raising=False)
    assert Settings().vat_exemption_purchase_tax_ids == {}

    monkeypatch.setenv("VAT_EXEMPTION_PURCHASE_TAX_IDS", '{"335": 9335}')
    assert Settings().vat_exemption_purchase_tax_ids == {"335": 9335}

    for bad in ('{"209": 9209}', '{"351": 9351}', '{"335": 6}', '{"335": 0}', '{"335": "abc"}'):
        monkeypatch.setenv("VAT_EXEMPTION_PURCHASE_TAX_IDS", bad)
        with pytest.raises(ValidationError):
            Settings()


def test_policy_refuses_a_configuration_without_a_reader() -> None:
    with pytest.raises(ValueError, match="tax reader"):
        VatExemptionTaxPolicy(tax_ids_by_code={"335": TAX_ID})
    with pytest.raises(VatExemptionConfigError):
        VatExemptionTaxPolicy(tax_ids_by_code={"209": TAX_ID}, tax_reader=FakeTaxReader())


def test_empty_configuration_composes_no_odoo_client() -> None:
    assert build_vat_exemption_tax_policy(settings=Settings(vat_exemption_purchase_tax_ids={})) is (
        DISABLED_VAT_EXEMPTION_POLICY
    )
    enabled = build_vat_exemption_tax_policy(settings=Settings(vat_exemption_purchase_tax_ids={"335": TAX_ID}))
    assert enabled.enabled and enabled.exemption_tax_ids == frozenset({TAX_ID})


# ===================================================================== 26: serialization


def test_26_exemption_result_round_trips_through_execution_evidence() -> None:
    invoice, assessment = invoice_and_assessment(document((Sub(),), (Sub(taxable="10.00"),)))
    result = engine(enabled_policy(), invoice).evaluate(command(invoice, assessment))

    data = _tax_match_to_data(result.tax_match)
    assert _tax_match_from_data(data) == result.tax_match
    assert data["line_results"][0]["result"]["tax_type"] == "EXEMPTION"


# ===================================================================== 27-29: Vendor Bill backstop


def pinned_source(content: bytes = document(), *, tax_match: InvoiceTaxMappingResult | None = None):
    invoice = parse_ubl_invoice(content)
    base = _source()
    invoice = replace(invoice, header=replace(invoice.header, ettn=base.source_invoice_id))
    pinned = tax_match or InvoiceTaxMappingResult(
        line_results=tuple(
            InvoiceTaxLineResult(
                line_number=line.line_number,
                tax_index=index,
                result=exemption_match_result(code="335", tax_id=TAX_ID, company_id=COMPANY_ID),
            )
            for line in invoice.lines
            for index, _ in enumerate(line.taxes)
        )
    )
    return replace(base, invoice=invoice, tax_match=pinned)


def safety_check(content: bytes = document(), *, policy: VatExemptionTaxPolicy | None = None, error=None):
    facts_reader = FixedSourceTaxFactsReader(
        SourceTaxAssessment.verified(extract_source_tax_facts(content)) if error is None else None, error=error
    )
    return SourceTaxSafetyCheck(facts_reader=facts_reader, exemption_policy=policy or enabled_policy())


def run_strategy(check: SourceTaxSafetyCheck, source, mode: ExecutionMode = ExecutionMode.EXECUTE):
    builder = RecordingVendorBillBuilder()
    writer = RecordingVendorBillWriter()
    strategy = VendorBillExecutionStrategy(
        source_invoice_reader=RecordingSourceInvoiceReader(source=source),
        vendor_bill_builder=builder,
        vendor_bill_writer=writer,
        resale_accounting_check=NON_RESALE_ACCOUNTING_CHECK,
        currency_reader=TWO_DECIMAL_CURRENCY_READER,
        source_tax_safety_check=check,
    )
    return strategy.execute(_step_request(mode=mode, approved_by="finance.lead")), builder, writer


@pytest.mark.parametrize("mode", [ExecutionMode.DRY_RUN, ExecutionMode.EXECUTE])
def test_valid_exemption_pin_is_re_proven_and_executes(mode: ExecutionMode, monkeypatch: pytest.MonkeyPatch) -> None:
    # The recording builder returns a fixed bill; money reconciliation is not under test here.
    monkeypatch.setattr("app.application.execution.vendor_bill_strategy.vendor_bill_monetary_errors", lambda *_: ())
    reader = FakeTaxReader()

    result, builder, writer = run_strategy(safety_check(policy=enabled_policy(reader)), pinned_source(), mode)

    assert result.status in {ExecutionStepStatus.DRY_RUN_OK, ExecutionStepStatus.EXECUTED}
    assert builder.calls == 1 and len(writer.commands) == 1
    assert reader.calls == [TAX_ID]


def test_27_execution_with_a_changed_tax_id_is_blocked() -> None:
    reader = FakeTaxReader(approved_tax_record(id=OTHER_TAX_ID))
    check = safety_check(policy=enabled_policy(reader, tax_id=OTHER_TAX_ID))

    result, builder, writer = run_strategy(check, pinned_source())

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == SOURCE_TAX_BLOCKED_ERROR_CODE
    assert builder.calls == 0 and writer.commands == ()
    assert reader.calls == []


@pytest.mark.parametrize(
    "overrides",
    [{"price_include": True}, {"active": False}, {"amount": 1.0}, {"children_tax_ids": [1]}, {"name": "0%"}],
)
def test_28_execution_with_a_changed_odoo_tax_is_blocked(overrides: dict[str, object]) -> None:
    reader = FakeTaxReader(approved_tax_record(**overrides))

    result, builder, writer = run_strategy(safety_check(policy=enabled_policy(reader)), pinned_source())

    assert result.error_code == SOURCE_TAX_BLOCKED_ERROR_CODE
    assert builder.calls == 0 and writer.commands == ()
    assert reader.calls == [TAX_ID]


def test_28_execution_with_a_deleted_odoo_tax_is_blocked() -> None:
    reader = FakeTaxReader()
    reader.missing = True

    result, builder, _ = run_strategy(safety_check(policy=enabled_policy(reader)), pinned_source())

    assert result.error_code == SOURCE_TAX_BLOCKED_ERROR_CODE and builder.calls == 0


def test_execution_odoo_outage_is_retryable() -> None:
    reader = FakeTaxReader(error=ErpRepositoryError("Odoo unavailable."))

    result, builder, _ = run_strategy(safety_check(policy=enabled_policy(reader)), pinned_source())

    assert result.error_code == SOURCE_TAX_UNAVAILABLE_ERROR_CODE and builder.calls == 0


def test_execution_after_the_configuration_was_removed_is_blocked() -> None:
    result, builder, _ = run_strategy(safety_check(policy=DISABLED_VAT_EXEMPTION_POLICY), pinned_source())

    assert result.error_code == SOURCE_TAX_BLOCKED_ERROR_CODE and builder.calls == 0


@pytest.mark.parametrize(
    "current_document",
    [document((Sub(exemption_code="209"),)), document(invoice_type="SATIS"), document((Sub(amount="1.00"),))],
    ids=["code-changed", "type-changed", "amount-changed"],
)
def test_execution_re_proves_the_exemption_from_the_current_document(current_document: bytes) -> None:
    reader = FakeTaxReader()
    check = safety_check(current_document, policy=enabled_policy(reader))

    result, builder, _ = run_strategy(check, pinned_source())

    assert result.error_code == SOURCE_TAX_BLOCKED_ERROR_CODE and builder.calls == 0
    assert reader.calls == []


def _vat_result(tax_id: int) -> TaxMatchResult:
    return TaxMatchResult(
        status=TaxMatchStatus.MATCHED,
        tax_id=tax_id,
        company_id=COMPANY_ID,
        tax_type=TaxType.VAT,
        tax_rate=Decimal("20"),
        matched_by="company_type_rate",
        confidence=Decimal("1.00"),
        reason="Exact company, type, and rate match.",
        candidate_count=1,
    )


def test_29_non_exempt_line_pinned_to_the_exemption_tax_is_blocked() -> None:
    content = document(invoice_type="SATIS")
    invoice = parse_ubl_invoice(content)
    tax_match = InvoiceTaxMappingResult(
        line_results=(
            InvoiceTaxLineResult(line_number=invoice.lines[0].line_number, tax_index=0, result=_vat_result(TAX_ID)),
        )
    )

    result, builder, _ = run_strategy(safety_check(content), pinned_source(content, tax_match=tax_match))

    assert result.error_code == SOURCE_TAX_BLOCKED_ERROR_CODE and builder.calls == 0


def test_29_mixed_pin_with_one_ordinary_vat_line_is_blocked() -> None:
    content = document((Sub(),), (Sub(taxable="10.00"),))
    invoice = parse_ubl_invoice(content)
    exempt = exemption_match_result(code="335", tax_id=TAX_ID, company_id=COMPANY_ID)
    tax_match = InvoiceTaxMappingResult(
        line_results=(
            InvoiceTaxLineResult(line_number=invoice.lines[0].line_number, tax_index=0, result=exempt),
            InvoiceTaxLineResult(line_number=invoice.lines[1].line_number, tax_index=0, result=_vat_result(34)),
        )
    )

    result, builder, _ = run_strategy(safety_check(content), pinned_source(content, tax_match=tax_match))

    assert result.error_code == SOURCE_TAX_BLOCKED_ERROR_CODE and builder.calls == 0


@pytest.mark.parametrize(
    "tampered",
    [
        {"tax_rate": Decimal("1")},
        {"matched_by": "company_type_rate"},
        {"company_id": 8},
        {"status": TaxMatchStatus.INVALID_INPUT},
        {"tax_id": GENERIC_ZERO_TAX_ID},
    ],
)
def test_tampered_exemption_pins_are_blocked(tampered: dict[str, object]) -> None:
    pin = replace(exemption_match_result(code="335", tax_id=TAX_ID, company_id=COMPANY_ID), **tampered)
    invoice = parse_ubl_invoice(document())
    tax_match = InvoiceTaxMappingResult(
        line_results=(InvoiceTaxLineResult(line_number=invoice.lines[0].line_number, tax_index=0, result=pin),)
    )

    result, builder, _ = run_strategy(safety_check(), pinned_source(tax_match=tax_match))

    assert result.error_code == SOURCE_TAX_BLOCKED_ERROR_CODE and builder.calls == 0


def test_ordinary_vat_execution_never_reads_the_exemption_tax() -> None:
    reader = FakeTaxReader(error=AssertionError("must not be read"))
    check = SourceTaxSafetyCheck(
        facts_reader=FixedSourceTaxFactsReader(
            SourceTaxAssessment.verified(extract_source_tax_facts(document(invoice_type="SATIS")))
        ),
        exemption_policy=enabled_policy(reader),
    )

    result, builder, _ = run_strategy(check, _source())

    assert result.status is ExecutionStepStatus.EXECUTED and builder.calls == 1
    assert reader.calls == []


def test_decision_acceptance_re_proves_exemption_pins() -> None:
    evidence = replace(_source_evidence(), invoice=pinned_source().invoice, tax_match=pinned_source().tax_match)
    writer = RecordingDecisionWriter()
    blocked = SubmitReviewDecisionUseCase(
        review_decision_writer=writer,
        unit_of_work=_FakeUnitOfWork(),
        execution_evidence_reader=RecordingEvidenceReader(result=evidence),
        source_tax_safety_check=safety_check(policy=DISABLED_VAT_EXEMPTION_POLICY),
    )

    with pytest.raises(SourceTaxSafetyBlockedError) as raised:
        blocked.execute(_select_workflow_command())

    assert raised.value.reason_codes == (UNSUPPORTED,)
    assert writer.commands_with_evidence == ()


def test_decision_acceptance_odoo_outage_is_retryable() -> None:
    evidence = replace(_source_evidence(), invoice=pinned_source().invoice, tax_match=pinned_source().tax_match)
    writer = RecordingDecisionWriter()
    use_case = SubmitReviewDecisionUseCase(
        review_decision_writer=writer,
        unit_of_work=_FakeUnitOfWork(),
        execution_evidence_reader=RecordingEvidenceReader(result=evidence),
        source_tax_safety_check=safety_check(
            policy=enabled_policy(FakeTaxReader(error=ErpRepositoryError("Odoo unavailable.")))
        ),
    )

    with pytest.raises(SourceTaxFactsUnavailableError):
        use_case.execute(_select_workflow_command())

    assert writer.commands_with_evidence == ()


def test_preview_re_proves_exemption_pins() -> None:
    from tests.unit.test_vendor_bill_preview import _product_decision, _product_source, _request
    from tests.unit.test_vendor_bill_preview import _use_case as _preview_use_case

    pinned = pinned_source()
    source = replace(
        _product_source(),
        invoice=replace(
            pinned.invoice, header=replace(pinned.invoice.header, ettn=_product_source().source_invoice_id)
        ),
        tax_match=pinned.tax_match,
    )
    use_case, _, _ = _preview_use_case(decision=_product_decision(), source=source)
    use_case._source_tax_safety_check = safety_check(policy=DISABLED_VAT_EXEMPTION_POLICY)

    with pytest.raises(SourceTaxSafetyBlockedError):
        use_case.preview(_request(review_id="review-product-1", decision_version=1))
