"""PR R: deterministic return-invoice reference matching through the lookup use case."""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from app.application.return_reference import (
    LookupReturnReferenceUseCase,
    OutboxInvoiceCandidate,
    OutboxLookupPage,
    OutboxLookupUnavailableError,
    ReturnReferenceCheck,
    ReturnReferenceError,
    ReturnReferenceIssue,
    ReturnReferenceLookupQuery,
    ReturnReferenceLookupResult,
    ReturnReferenceLookupUnavailableError,
    ReturnReferenceNotApplicableError,
    ReturnReferenceSourceDocumentError,
    ReturnReferenceStatus,
    SearchOutcome,
)
from app.application.return_reference.matching import reference_status
from app.application.workbench.exceptions import ReviewNotFoundError, WorkbenchContractError
from app.domain.invoice import parse_ubl_invoice
from app.domain.invoice.document_types import is_return_invoice
from tests.unit.pr_r_fixtures import (
    COMPANY_ID,
    COMPANY_VKN,
    CUSTOMER_VKN,
    ORIGINAL_ETTN,
    REFERENCE_DATE,
    REFERENCE_NUMBER,
    RETURN_UUID,
    SECOND_ORIGINAL_ETTN,
    SECOND_REFERENCE_NUMBER,
    return_ubl,
)

REVIEW_ID = "review:pr-r-synthetic-0001"
STORAGE_KEY = "uyumsoft/inbox/synthetic.xml"
Status = ReturnReferenceStatus
Check = ReturnReferenceCheck
Issue = ReturnReferenceIssue


def _candidate(**overrides: object) -> OutboxInvoiceCandidate:
    values: dict[str, object] = {
        "invoice_number": REFERENCE_NUMBER,
        "ettn": ORIGINAL_ETTN,
        "invoice_type": "Sales",
        "scenario": "ComercialInvoice",
        "status": "Approved",
        "buyer_tax_number": CUSTOMER_VKN,
        "currency": "TRY",
        "issue_date": date(2026, 9, 10),
        "archived": False,
    }
    values.update(overrides)
    return OutboxInvoiceCandidate(**values)  # type: ignore[arg-type]


PAGE_SIZE = 10
EMPTY = OutboxLookupPage(candidates=(), provider_total=0, page_size=PAGE_SIZE)
_COUNTED = object()


def _page(
    *candidates: OutboxInvoiceCandidate, total: int | None | object = _COUNTED, page_size: int = PAGE_SIZE
) -> OutboxLookupPage:
    """``total`` defaults to the row count; pass ``None`` for a provider response without TotalCount."""

    provider_total = len(candidates) if total is _COUNTED else total
    return OutboxLookupPage(candidates=candidates, provider_total=provider_total, page_size=page_size)  # type: ignore[arg-type]


def _filler(count: int) -> tuple[OutboxInvoiceCandidate, ...]:
    """Non-exact provider rows (ignored as candidates) that fill a page."""

    return tuple(_candidate(invoice_number=f"OTH2026{index:09d}", ettn=None) for index in range(count))


class _OutboxLookup:
    """Results keyed by (invoice_number, archived); missing keys are empty pages."""

    def __init__(self, results: dict[tuple[str, bool], OutboxLookupPage | Exception] | None = None) -> None:
        self.results = results or {}
        self.calls: list[tuple[str, date, bool]] = []

    def find_by_invoice_number(self, invoice_number: str, *, issue_date: date, archived: bool) -> OutboxLookupPage:
        self.calls.append((invoice_number, issue_date, archived))
        result = self.results.get((invoice_number, archived), EMPTY)
        if isinstance(result, Exception):
            raise result
        return result


class _CompanyResolver:
    def __init__(self, ids: tuple[int, ...] | Exception = (COMPANY_ID,)) -> None:
        self.ids = ids
        self.calls: list[str] = []

    def company_ids_for_tax_number(self, tax_number: str) -> tuple[int, ...]:
        self.calls.append(tax_number)
        if isinstance(self.ids, Exception):
            raise self.ids
        return self.ids


class _Reviews:
    def get_review_item(self, query):
        if query.review_id != REVIEW_ID or query.company_id != COMPANY_ID:
            raise ReviewNotFoundError("Review item was not found.")
        return SimpleNamespace(review_id=REVIEW_ID, version=3)


class _Sources:
    def __init__(self, xml: bytes) -> None:
        self.invoice = parse_ubl_invoice(xml)

    def get(self, *, review_id: str, company_id: int):
        return SimpleNamespace(invoice=self.invoice, source_invoice_id=self.invoice.header.invoice_uuid)


class _Documents:
    def __init__(self, content: bytes | Exception, *, count: int = 1, sha256: str | None = None) -> None:
        self.content = content
        digest = sha256 or (hashlib.sha256(content).hexdigest() if isinstance(content, bytes) else "0" * 64)
        self.documents = tuple(SimpleNamespace(storage_key=STORAGE_KEY, content_sha256=digest) for _ in range(count))

    def find_source_documents(self, *, review_id: str, company_id: int):
        return self.documents

    def read(self, storage_key: str) -> bytes:
        if isinstance(self.content, Exception):
            raise self.content
        return self.content


def _lookup(
    xml: bytes | None = None,
    *,
    outbox: _OutboxLookup | None = None,
    company: _CompanyResolver | None = None,
    stored: bytes | Exception | None = None,
    documents: _Documents | None = None,
    account_company_id: int | None = COMPANY_ID,
) -> tuple[ReturnReferenceLookupResult, _OutboxLookup, _CompanyResolver]:
    xml = xml if xml is not None else return_ubl()
    outbox = outbox or _OutboxLookup({(REFERENCE_NUMBER, False): _page(_candidate())})
    company = company or _CompanyResolver()
    documents = documents or _Documents(stored if stored is not None else xml)
    use_case = LookupReturnReferenceUseCase(
        review_reader=_Reviews(),
        source_reader=_Sources(xml),
        document_locator=documents,
        document_reader=documents,
        company_resolver=company,
        outbox_lookup=outbox,
        outbox_account_company_id=account_company_id,
    )
    result = use_case.execute(ReturnReferenceLookupQuery(review_id=REVIEW_ID, company_id=COMPANY_ID))
    return result, outbox, company


def _only_reference(result: ReturnReferenceLookupResult):
    (reference,) = result.references
    return reference


# ------------------------------------------------------------------ outcomes


def test_exact_match_searches_active_and_archived_and_matches() -> None:
    result, outbox, company = _lookup()

    assert result.status is Status.MATCHED
    assert (result.review_id, result.company_id, result.review_version) == (REVIEW_ID, COMPANY_ID, 3)
    assert result.return_invoice_type_code == "IADE"
    assert result.company_identity_verified is True
    assert company.calls == [COMPANY_VKN]
    assert outbox.calls == [
        (REFERENCE_NUMBER, date(2026, 9, 10), False),
        (REFERENCE_NUMBER, date(2026, 9, 10), True),
    ]
    assert result.provider_calls == 2
    reference = _only_reference(result)
    assert reference.status is Status.MATCHED
    assert reference.failed_checks == ()
    assert [(search.archived, search.completed, search.exact) for search in reference.searches] == [
        (False, True, 1),
        (True, True, 0),
    ]
    (evaluation,) = reference.candidates
    assert evaluation.candidate.ettn == ORIGINAL_ETTN
    assert {item.check for item in evaluation.checks} == set(Check)
    assert all(item.passed for item in evaluation.checks)


def test_no_match_in_either_partition_is_not_found() -> None:
    result, outbox, _ = _lookup(outbox=_OutboxLookup())

    assert result.status is Status.NOT_FOUND
    assert len(outbox.calls) == 2
    assert _only_reference(result).candidates == ()


def test_multiple_exact_matches_are_ambiguous_and_never_auto_selected() -> None:
    outbox = _OutboxLookup({(REFERENCE_NUMBER, False): _page(_candidate(), _candidate(ettn=SECOND_ORIGINAL_ETTN))})

    result, _, _ = _lookup(outbox=outbox)

    reference = _only_reference(result)
    assert result.status is Status.AMBIGUOUS
    assert Issue.MULTIPLE_CANDIDATES in reference.issues
    assert len(reference.candidates) == 2
    assert reference.failed_checks == ()


def test_same_invoice_in_both_partitions_counts_once() -> None:
    outbox = _OutboxLookup(
        {(REFERENCE_NUMBER, False): _page(_candidate()), (REFERENCE_NUMBER, True): _page(_candidate())}
    )

    result, _, _ = _lookup(outbox=outbox)

    assert result.status is Status.MATCHED
    assert len(_only_reference(result).candidates) == 1


def test_archived_original_is_found_by_the_archived_search() -> None:
    outbox = _OutboxLookup({(REFERENCE_NUMBER, True): _page(_candidate(archived=True))})

    result, _, _ = _lookup(outbox=outbox)

    assert result.status is Status.MATCHED
    assert _only_reference(result).candidates[0].candidate.archived is True


@pytest.mark.parametrize(
    ("overrides", "failed"),
    [
        ({"status": "Canceled"}, Check.INVOICE_STATUS),
        ({"status": "Declined"}, Check.INVOICE_STATUS),
        ({"status": None}, Check.INVOICE_STATUS),
        ({"buyer_tax_number": "9000000009"}, Check.BUYER_IDENTITY),
        ({"buyer_tax_number": None}, Check.BUYER_IDENTITY),
        ({"currency": "USD"}, Check.CURRENCY),
        ({"issue_date": date(2026, 9, 11)}, Check.ORIGINAL_DATE),
        ({"issue_date": None}, Check.ORIGINAL_DATE),
        ({"invoice_type": "Return"}, Check.INVOICE_TYPE),
        ({"invoice_type": "Tax"}, Check.INVOICE_TYPE),
    ],
)
def test_single_candidate_failing_a_check_is_a_mismatch(overrides: dict[str, object], failed: Check) -> None:
    outbox = _OutboxLookup({(REFERENCE_NUMBER, False): _page(_candidate(**overrides))})

    result, _, _ = _lookup(outbox=outbox)

    reference = _only_reference(result)
    assert result.status is Status.MISMATCH
    assert reference.failed_checks == (failed,)


def test_buyer_identity_ignores_formatting_but_not_digits() -> None:
    outbox = _OutboxLookup({(REFERENCE_NUMBER, False): _page(_candidate(buyer_tax_number=f" {CUSTOMER_VKN} "))})

    assert _lookup(outbox=outbox)[0].status is Status.MATCHED


def test_original_dated_after_the_return_is_a_mismatch() -> None:
    xml = return_ubl(references=((REFERENCE_NUMBER, "2026-09-20", "IADE"),), issue_date="2026-09-15")
    outbox = _OutboxLookup({(REFERENCE_NUMBER, False): _page(_candidate(issue_date=date(2026, 9, 20)))})

    result, _, _ = _lookup(xml, outbox=outbox)

    assert result.status is Status.MISMATCH
    assert _only_reference(result).failed_checks == (Check.ORIGINAL_DATE,)


def test_unverified_return_variant_fails_the_invoice_type_check() -> None:
    xml = return_ubl(invoice_type="TEVKIFATIADE")

    result, _, _ = _lookup(xml)

    assert result.return_invoice_type_code == "TEVKIFATIADE"
    assert result.status is Status.MISMATCH
    assert _only_reference(result).failed_checks == (Check.INVOICE_TYPE,)


def test_company_identity_unavailable_raises_instead_of_concluding() -> None:
    with pytest.raises(ReturnReferenceLookupUnavailableError):
        _lookup(company=_CompanyResolver(ReturnReferenceLookupUnavailableError("down")))


# ------------------------------------------------------------------ company isolation (fail-closed)

OTHER_COMPANY_ID = 99


def _assert_fail_closed(result: ReturnReferenceLookupResult, outbox: _OutboxLookup, issue: Issue) -> None:
    assert result.status is Status.COMPANY_UNVERIFIED
    assert result.issues == (issue,)
    assert result.company_identity_verified is False
    assert result.provider_calls == 0
    assert outbox.calls == []
    for reference in result.references:
        assert reference.candidates == ()
        assert reference.searches == ()
        assert reference.failed_checks == ()


def _outbox_with_a_hit() -> _OutboxLookup:
    """An outbox that *would* expose a valid candidate if it were ever searched."""

    return _OutboxLookup({(REFERENCE_NUMBER, False): _page(_candidate())})


@pytest.mark.parametrize("account_company_id", [None, OTHER_COMPANY_ID])
def test_outbox_account_not_owned_by_requesting_company_is_never_searched(account_company_id: int | None) -> None:
    """Cross-company: the configured Uyumsoft account is unbound or belongs to another company."""

    company = _CompanyResolver()
    result, outbox, _ = _lookup(outbox=_outbox_with_a_hit(), company=company, account_company_id=account_company_id)

    _assert_fail_closed(result, outbox, Issue.OUTBOX_ACCOUNT_OWNER_UNVERIFIED)
    assert _only_reference(result).status is Status.COMPANY_UNVERIFIED
    assert company.calls == []


def test_unbound_account_fails_closed_even_when_the_erp_is_unavailable() -> None:
    result, outbox, company = _lookup(
        company=_CompanyResolver(ReturnReferenceLookupUnavailableError("down")), account_company_id=None
    )

    _assert_fail_closed(result, outbox, Issue.OUTBOX_ACCOUNT_OWNER_UNVERIFIED)
    assert company.calls == []


@pytest.mark.parametrize("company_ids", [(OTHER_COMPANY_ID,), (COMPANY_ID, OTHER_COMPANY_ID), ()])
def test_return_buyer_not_exactly_the_requesting_company_is_never_searched(company_ids: tuple[int, ...]) -> None:
    """Cross-company: the return was addressed to another company (or an ambiguous / unknown one)."""

    result, outbox, company = _lookup(outbox=_outbox_with_a_hit(), company=_CompanyResolver(company_ids))

    _assert_fail_closed(result, outbox, Issue.COMPANY_IDENTITY_UNVERIFIED)
    assert company.calls == [COMPANY_VKN]


def test_return_without_buyer_tax_number_is_never_searched() -> None:
    result, outbox, company = _lookup(return_ubl(customer_vkn=""), outbox=_outbox_with_a_hit())

    _assert_fail_closed(result, outbox, Issue.COMPANY_IDENTITY_UNVERIFIED)
    assert company.calls == []


def test_company_unverified_keeps_reference_format_problems_visible() -> None:
    xml = return_ubl(references=((REFERENCE_NUMBER, REFERENCE_DATE, "IADE"), (None, REFERENCE_DATE, "IADE")))

    result, outbox, _ = _lookup(xml, account_company_id=None)

    _assert_fail_closed(result, outbox, Issue.OUTBOX_ACCOUNT_OWNER_UNVERIFIED)
    assert [reference.status for reference in result.references] == [
        Status.COMPANY_UNVERIFIED,
        Status.REFERENCE_UNPARSEABLE,
    ]
    assert result.references[1].issues == (Issue.INVOICE_NUMBER_MISSING,)


def test_only_unparseable_references_need_no_company_verification() -> None:
    result, outbox, company = _lookup(return_ubl(references=((None, REFERENCE_DATE, "IADE"),)), account_company_id=None)

    assert result.status is Status.REFERENCE_UNPARSEABLE
    assert result.company_identity_verified is None
    assert (outbox.calls, company.calls) == ([], [])


def test_verified_company_reports_outbox_data() -> None:
    result, outbox, company = _lookup()

    assert result.company_identity_verified is True
    assert company.calls == [COMPANY_VKN]
    assert len(outbox.calls) == 2
    assert len(_only_reference(result).candidates) == 1


@pytest.mark.parametrize("verified", [False, None])
def test_result_cannot_carry_outbox_data_without_a_verified_company(verified: bool | None) -> None:
    result, _, _ = _lookup()

    with pytest.raises(ReturnReferenceError):
        replace(result, company_identity_verified=verified)


# ------------------------------------------------------------------ search completeness


def test_provider_failure_in_both_partitions_is_incomplete() -> None:
    outbox = _OutboxLookup(
        {
            (REFERENCE_NUMBER, False): OutboxLookupUnavailableError("down"),
            (REFERENCE_NUMBER, True): OutboxLookupUnavailableError("down"),
        }
    )

    result, _, _ = _lookup(outbox=outbox)

    reference = _only_reference(result)
    assert result.status is Status.INCOMPLETE_SEARCH
    assert reference.issues == (Issue.ACTIVE_SEARCH_FAILED, Issue.ARCHIVED_SEARCH_FAILED)
    assert [search.completed for search in reference.searches] == [False, False]


def test_archive_failure_never_yields_not_found() -> None:
    outbox = _OutboxLookup({(REFERENCE_NUMBER, True): OutboxLookupUnavailableError("down")})

    result, _, _ = _lookup(outbox=outbox)

    assert result.status is Status.INCOMPLETE_SEARCH
    assert _only_reference(result).issues == (Issue.ARCHIVED_SEARCH_FAILED,)


def test_archive_failure_never_yields_matched_even_with_an_active_hit() -> None:
    outbox = _OutboxLookup(
        {(REFERENCE_NUMBER, False): _page(_candidate()), (REFERENCE_NUMBER, True): OutboxLookupUnavailableError("x")}
    )

    result, _, _ = _lookup(outbox=outbox)

    assert result.status is Status.INCOMPLETE_SEARCH
    assert len(_only_reference(result).candidates) == 1


def test_active_failure_with_archived_miss_is_incomplete() -> None:
    outbox = _OutboxLookup({(REFERENCE_NUMBER, False): OutboxLookupUnavailableError("down")})

    result, _, _ = _lookup(outbox=outbox)

    assert result.status is Status.INCOMPLETE_SEARCH
    assert _only_reference(result).issues == (Issue.ACTIVE_SEARCH_FAILED,)


def test_non_exact_provider_rows_are_ignored() -> None:
    outbox = _OutboxLookup(
        {
            (REFERENCE_NUMBER, False): _page(
                _candidate(invoice_number=f"{REFERENCE_NUMBER}9"), _candidate(invoice_number=None)
            )
        }
    )

    result, _, _ = _lookup(outbox=outbox)

    reference = _only_reference(result)
    assert result.status is Status.NOT_FOUND
    assert (reference.searches[0].returned, reference.searches[0].exact) == (2, 0)


def test_full_page_without_provider_total_is_incomplete_not_not_found() -> None:
    outbox = _OutboxLookup({(REFERENCE_NUMBER, False): _page(*_filler(PAGE_SIZE), total=None)})

    result, _, _ = _lookup(outbox=outbox)

    reference = _only_reference(result)
    assert result.status is Status.INCOMPLETE_SEARCH
    assert reference.status is Status.INCOMPLETE_SEARCH
    assert reference.issues == (Issue.PROVIDER_TOTAL_MISSING,)
    assert (reference.searches[0].returned, reference.searches[0].page_size) == (PAGE_SIZE, PAGE_SIZE)


def test_full_archived_page_without_provider_total_is_incomplete() -> None:
    outbox = _OutboxLookup({(REFERENCE_NUMBER, True): _page(*_filler(PAGE_SIZE), total=None)})

    result, _, _ = _lookup(outbox=outbox)

    assert result.status is Status.INCOMPLETE_SEARCH
    assert _only_reference(result).issues == (Issue.PROVIDER_TOTAL_MISSING,)


def test_full_page_without_provider_total_never_yields_matched() -> None:
    outbox = _OutboxLookup({(REFERENCE_NUMBER, False): _page(_candidate(), *_filler(PAGE_SIZE - 1), total=None)})

    result, _, _ = _lookup(outbox=outbox)

    reference = _only_reference(result)
    assert result.status is Status.INCOMPLETE_SEARCH
    assert reference.issues == (Issue.PROVIDER_TOTAL_MISSING,)
    assert len(reference.candidates) == 1


def test_confirmed_multiple_candidates_stay_ambiguous_on_a_full_page_without_total() -> None:
    second = _candidate(ettn=SECOND_ORIGINAL_ETTN)
    outbox = _OutboxLookup(
        {(REFERENCE_NUMBER, False): _page(_candidate(), second, *_filler(PAGE_SIZE - 2), total=None)}
    )

    result, _, _ = _lookup(outbox=outbox)

    reference = _only_reference(result)
    assert result.status is Status.AMBIGUOUS
    assert reference.issues == (Issue.PROVIDER_TOTAL_MISSING, Issue.MULTIPLE_CANDIDATES)
    assert reference.failed_checks == ()


def test_short_page_without_provider_total_is_definitive() -> None:
    outbox = _OutboxLookup({(REFERENCE_NUMBER, False): _page(*_filler(PAGE_SIZE - 1), total=None)})

    result, _, _ = _lookup(outbox=outbox)

    assert result.status is Status.NOT_FOUND
    assert _only_reference(result).issues == ()


def test_short_page_without_provider_total_can_match() -> None:
    outbox = _OutboxLookup({(REFERENCE_NUMBER, False): _page(_candidate(), total=None)})

    result, _, _ = _lookup(outbox=outbox)

    assert result.status is Status.MATCHED


def test_full_page_with_matching_provider_total_is_definitive() -> None:
    outbox = _OutboxLookup({(REFERENCE_NUMBER, False): _page(*_filler(PAGE_SIZE), total=PAGE_SIZE)})

    result, _, _ = _lookup(outbox=outbox)

    assert result.status is Status.NOT_FOUND


def test_search_with_unknown_page_size_and_no_total_is_treated_as_incomplete() -> None:
    searches = (
        SearchOutcome(archived=False, completed=True, provider_total=None, returned=0, page_size=None),
        SearchOutcome(archived=True, completed=True, provider_total=0, returned=0, page_size=PAGE_SIZE),
    )

    assert reference_status(searches=searches, evaluations=()) == (
        Status.INCOMPLETE_SEARCH,
        (Issue.PROVIDER_TOTAL_MISSING,),
    )


def test_truncated_provider_page_is_ambiguous() -> None:
    outbox = _OutboxLookup({(REFERENCE_NUMBER, False): _page(_candidate(), total=11)})

    result, _, _ = _lookup(outbox=outbox)

    assert result.status is Status.AMBIGUOUS
    assert _only_reference(result).issues == (Issue.PROVIDER_RESULT_TRUNCATED,)


# ------------------------------------------------------------------ multiple references


def test_multiple_references_all_matched_is_matched() -> None:
    xml = return_ubl(
        references=((REFERENCE_NUMBER, REFERENCE_DATE, "IADE"), (SECOND_REFERENCE_NUMBER, REFERENCE_DATE, "IADE"))
    )
    outbox = _OutboxLookup(
        {
            (REFERENCE_NUMBER, False): _page(_candidate()),
            (SECOND_REFERENCE_NUMBER, False): _page(
                _candidate(invoice_number=SECOND_REFERENCE_NUMBER, ettn=SECOND_ORIGINAL_ETTN)
            ),
        }
    )

    result, _, _ = _lookup(xml, outbox=outbox)

    assert result.status is Status.MATCHED
    assert [reference.status for reference in result.references] == [Status.MATCHED, Status.MATCHED]
    assert result.provider_calls == 4


def test_one_unmatched_reference_prevents_overall_match() -> None:
    xml = return_ubl(
        references=((REFERENCE_NUMBER, REFERENCE_DATE, "IADE"), (SECOND_REFERENCE_NUMBER, REFERENCE_DATE, "IADE"))
    )

    result, _, _ = _lookup(xml)

    assert [reference.status for reference in result.references] == [Status.MATCHED, Status.NOT_FOUND]
    assert result.status is Status.NOT_FOUND


def test_overall_status_precedence_prefers_incomplete_then_unparseable() -> None:
    xml = return_ubl(
        references=(
            (REFERENCE_NUMBER, REFERENCE_DATE, "IADE"),
            ("not-a-number", REFERENCE_DATE, "IADE"),
            (SECOND_REFERENCE_NUMBER, REFERENCE_DATE, "IADE"),
        )
    )
    outbox = _OutboxLookup(
        {
            (REFERENCE_NUMBER, False): _page(_candidate()),
            (SECOND_REFERENCE_NUMBER, True): OutboxLookupUnavailableError("down"),
        }
    )

    result, _, _ = _lookup(xml, outbox=outbox)

    assert [reference.status for reference in result.references] == [
        Status.MATCHED,
        Status.REFERENCE_UNPARSEABLE,
        Status.INCOMPLETE_SEARCH,
    ]
    assert result.status is Status.INCOMPLETE_SEARCH


def test_duplicate_reference_numbers_are_searched_once() -> None:
    xml = return_ubl(
        references=((REFERENCE_NUMBER, REFERENCE_DATE, "IADE"), (REFERENCE_NUMBER, REFERENCE_DATE, "IADE"))
    )

    result, outbox, _ = _lookup(xml)

    assert result.status is Status.MATCHED
    assert len(outbox.calls) == 2
    assert result.provider_calls == 2


# ------------------------------------------------------------------ reference / document problems


def test_missing_reference_makes_no_external_call() -> None:
    result, outbox, company = _lookup(return_ubl(references=()))

    assert result.status is Status.REFERENCE_MISSING
    assert result.references == ()
    assert outbox.calls == [] and company.calls == []
    assert result.provider_calls == 0


@pytest.mark.parametrize(
    ("reference", "issues"),
    [
        ((None, REFERENCE_DATE, "IADE"), (Issue.INVOICE_NUMBER_MISSING,)),
        ((REFERENCE_NUMBER.lower(), REFERENCE_DATE, "IADE"), (Issue.INVOICE_NUMBER_FORMAT_INVALID,)),
        ((RETURN_UUID, REFERENCE_DATE, "IADE"), (Issue.INVOICE_NUMBER_FORMAT_INVALID,)),
        ((REFERENCE_NUMBER, None, "IADE"), (Issue.ISSUE_DATE_MISSING,)),
        ((REFERENCE_NUMBER, "10/09/2026", "IADE"), (Issue.ISSUE_DATE_INVALID,)),
    ],
)
def test_unparseable_reference_is_never_searched(reference, issues) -> None:
    result, outbox, company = _lookup(return_ubl(references=(reference,)))

    assert result.status is Status.REFERENCE_UNPARSEABLE
    assert _only_reference(result).issues == issues
    assert outbox.calls == [] and company.calls == []


def test_malformed_stored_xml_is_unparseable() -> None:
    bad = b"<Invoice><broken>"

    result, outbox, _ = _lookup(stored=bad)

    assert result.status is Status.REFERENCE_UNPARSEABLE
    assert result.issues == (Issue.XML_INVALID,)
    assert outbox.calls == []


@pytest.mark.parametrize(
    ("documents", "issue"),
    [
        (_Documents(return_ubl(), count=0), Issue.SOURCE_DOCUMENT_MISSING),
        (_Documents(return_ubl(), count=2), Issue.SOURCE_DOCUMENT_AMBIGUOUS),
        (_Documents(return_ubl(), sha256="f" * 64), Issue.SOURCE_DOCUMENT_HASH_MISMATCH),
        (_Documents(ReturnReferenceSourceDocumentError("gone")), Issue.SOURCE_DOCUMENT_UNREADABLE),
        (
            _Documents(return_ubl(uuid="5a5a0000-0000-4000-8000-0000000000ff")),
            Issue.SOURCE_DOCUMENT_IDENTITY_MISMATCH,
        ),
    ],
)
def test_source_document_problems_are_unparseable(documents: _Documents, issue: Issue) -> None:
    result, outbox, _ = _lookup(documents=documents)

    assert result.status is Status.REFERENCE_UNPARSEABLE
    assert result.issues == (issue,)
    assert outbox.calls == []


# ------------------------------------------------------------------ applicability and safety


@pytest.mark.parametrize("invoice_type", ["SATIS", "TEVKIFAT", "ISTISNA"])
def test_non_return_invoice_is_not_applicable(invoice_type: str) -> None:
    with pytest.raises(ReturnReferenceNotApplicableError):
        _lookup(return_ubl(invoice_type=invoice_type))


def test_unknown_review_propagates_not_found() -> None:
    use_case = LookupReturnReferenceUseCase(
        review_reader=_Reviews(),
        source_reader=_Sources(return_ubl()),
        document_locator=_Documents(return_ubl()),
        document_reader=_Documents(return_ubl()),
        company_resolver=_CompanyResolver(),
        outbox_lookup=_OutboxLookup(),
        outbox_account_company_id=COMPANY_ID,
    )
    with pytest.raises(ReviewNotFoundError):
        use_case.execute(ReturnReferenceLookupQuery(review_id="review:other", company_id=COMPANY_ID))


@pytest.mark.parametrize(("review_id", "company_id"), [("", 7), ("  ", 7), (REVIEW_ID, 0), (REVIEW_ID, -1)])
def test_query_contract_is_validated(review_id: str, company_id: int) -> None:
    with pytest.raises(WorkbenchContractError):
        ReturnReferenceLookupQuery(review_id=review_id, company_id=company_id)


def test_use_case_depends_on_read_ports_only() -> None:
    """No writer, unit of work, decision or projection dependency: results cannot be persisted."""

    parameters = set(inspect.signature(LookupReturnReferenceUseCase.__init__).parameters) - {"self"}

    assert parameters == {
        "review_reader",
        "source_reader",
        "document_locator",
        "document_reader",
        "company_resolver",
        "outbox_lookup",
        "outbox_account_company_id",
    }


def test_matched_lookup_leaves_the_return_invoice_a_return() -> None:
    """PR S stays authoritative: a MATCHED lookup does not reinterpret the return as a purchase."""

    xml = return_ubl()
    sources = _Sources(xml)
    before = replace(sources.invoice.header)

    result, _, _ = _lookup(xml)

    assert result.status is Status.MATCHED
    assert sources.invoice.header == before
    assert is_return_invoice(sources.invoice)
