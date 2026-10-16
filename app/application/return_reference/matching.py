"""Deterministic return-reference matching rules (PR R). Pure functions, no I/O.

Rules:
- Only an exact (trimmed, case-sensitive) ``InvoiceId`` equal to the reference number is
  a candidate; anything else the provider returns is ignored. No fuzzy matching.
- Both the active and the archived outbox must have been searched successfully before a
  reference can be NOT_FOUND, MATCHED or MISMATCH; otherwise it is INCOMPLETE_SEARCH.
- More than one exact candidate, or a provider total larger than the page returned, is
  AMBIGUOUS. A candidate is never chosen automatically.
- A full page without a provider total may hide further rows: unless more than one exact
  candidate is already confirmed (AMBIGUOUS), the reference is INCOMPLETE_SEARCH.
- A single candidate is MATCHED only when every check passes, else MISMATCH.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date

from app.application.return_reference.model import (
    CandidateEvaluation,
    CheckResult,
    OutboxInvoiceCandidate,
    ReturnReferenceCheck,
    ReturnReferenceIssue,
    ReturnReferenceStatus,
    SearchOutcome,
)
from app.domain.invoice.billing_reference import BillingReference

#: Allowed original ``InvoiceTipType`` per return ``InvoiceTypeCode``. Only IADE -> Sales is
#: verified against production; other return variants fail INVOICE_TYPE until verified.
ORIGINAL_INVOICE_TYPES_BY_RETURN_CODE: dict[str, frozenset[str]] = {"IADE": frozenset({"Sales"})}
#: Provider statuses for a final, valid original invoice.
VALID_ORIGINAL_STATUSES = frozenset({"Approved"})

#: When several references disagree, the overall status is the first present in this order.
OVERALL_STATUS_PRECEDENCE = (
    ReturnReferenceStatus.COMPANY_UNVERIFIED,
    ReturnReferenceStatus.INCOMPLETE_SEARCH,
    ReturnReferenceStatus.REFERENCE_UNPARSEABLE,
    ReturnReferenceStatus.AMBIGUOUS,
    ReturnReferenceStatus.MISMATCH,
    ReturnReferenceStatus.NOT_FOUND,
)


def reference_format_issues(reference: BillingReference) -> tuple[ReturnReferenceIssue, ...]:
    issues: list[ReturnReferenceIssue] = []
    if reference.invoice_number is None:
        issues.append(ReturnReferenceIssue.INVOICE_NUMBER_MISSING)
    elif not reference.has_gib_invoice_number:
        issues.append(ReturnReferenceIssue.INVOICE_NUMBER_FORMAT_INVALID)
    if not reference.issue_date_present:
        issues.append(ReturnReferenceIssue.ISSUE_DATE_MISSING)
    elif reference.issue_date is None:
        issues.append(ReturnReferenceIssue.ISSUE_DATE_INVALID)
    return tuple(issues)


def exact_candidates(
    invoice_number: str, candidates: Iterable[OutboxInvoiceCandidate]
) -> tuple[OutboxInvoiceCandidate, ...]:
    return tuple(
        candidate
        for candidate in candidates
        if candidate.invoice_number is not None and candidate.invoice_number.strip() == invoice_number
    )


def deduplicate(candidates: Iterable[OutboxInvoiceCandidate]) -> tuple[OutboxInvoiceCandidate, ...]:
    """One entry per provider invoice (ETTN), preserving first-seen order."""

    seen: set[tuple[object, ...]] = set()
    unique: list[OutboxInvoiceCandidate] = []
    for candidate in candidates:
        key: tuple[object, ...] = (
            ("ettn", candidate.ettn.strip().lower())
            if candidate.ettn and candidate.ettn.strip()
            else ("fields", candidate.invoice_number, candidate.issue_date, candidate.archived)
        )
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return tuple(unique)


def search_issues(searches: Iterable[SearchOutcome]) -> tuple[ReturnReferenceIssue, ...]:
    issues: list[ReturnReferenceIssue] = []
    for search in searches:
        if not search.completed:
            failed = ReturnReferenceIssue.ACTIVE_SEARCH_FAILED
            if search.archived:
                failed = ReturnReferenceIssue.ARCHIVED_SEARCH_FAILED
            issues.append(failed)
        elif search.provider_total is not None and search.provider_total > search.returned:
            issues.append(ReturnReferenceIssue.PROVIDER_RESULT_TRUNCATED)
        elif search.provider_total is None and _page_full(search):
            issues.append(ReturnReferenceIssue.PROVIDER_TOTAL_MISSING)
    return tuple(dict.fromkeys(issues))


def evaluate_candidate(
    candidate: OutboxInvoiceCandidate,
    *,
    reference_issue_date: date,
    return_issue_date: date | None,
    return_type_code: str,
    return_seller_tax_number: str | None,
    return_currency: str | None,
    company_identity_verified: bool,
) -> CandidateEvaluation:
    checks = (
        CheckResult(ReturnReferenceCheck.COMPANY_IDENTITY, company_identity_verified),
        CheckResult(ReturnReferenceCheck.SELLER_IDENTITY, company_identity_verified),
        CheckResult(
            ReturnReferenceCheck.BUYER_IDENTITY,
            _same_tax_number(candidate.buyer_tax_number, return_seller_tax_number),
        ),
        CheckResult(ReturnReferenceCheck.CURRENCY, _same_code(candidate.currency, return_currency)),
        CheckResult(
            ReturnReferenceCheck.ORIGINAL_DATE,
            candidate.issue_date is not None
            and candidate.issue_date == reference_issue_date
            and return_issue_date is not None
            and candidate.issue_date <= return_issue_date,
        ),
        CheckResult(
            ReturnReferenceCheck.INVOICE_TYPE,
            candidate.invoice_type in ORIGINAL_INVOICE_TYPES_BY_RETURN_CODE.get(return_type_code, frozenset()),
        ),
        CheckResult(ReturnReferenceCheck.INVOICE_STATUS, candidate.status in VALID_ORIGINAL_STATUSES),
    )
    return CandidateEvaluation(candidate=candidate, checks=checks)


def reference_status(
    *,
    searches: tuple[SearchOutcome, ...],
    evaluations: tuple[CandidateEvaluation, ...],
) -> tuple[ReturnReferenceStatus, tuple[ReturnReferenceIssue, ...]]:
    issues = search_issues(searches)
    if any(not search.completed for search in searches):
        return ReturnReferenceStatus.INCOMPLETE_SEARCH, issues
    if len(evaluations) > 1:
        return ReturnReferenceStatus.AMBIGUOUS, (*issues, ReturnReferenceIssue.MULTIPLE_CANDIDATES)
    if ReturnReferenceIssue.PROVIDER_RESULT_TRUNCATED in issues:
        return ReturnReferenceStatus.AMBIGUOUS, issues
    if ReturnReferenceIssue.PROVIDER_TOTAL_MISSING in issues:
        return ReturnReferenceStatus.INCOMPLETE_SEARCH, issues
    if not evaluations:
        return ReturnReferenceStatus.NOT_FOUND, issues
    if evaluations[0].failed_checks:
        return ReturnReferenceStatus.MISMATCH, issues
    return ReturnReferenceStatus.MATCHED, issues


def overall_status(statuses: Iterable[ReturnReferenceStatus]) -> ReturnReferenceStatus:
    present = tuple(statuses)
    if not present:
        return ReturnReferenceStatus.REFERENCE_MISSING
    if all(status is ReturnReferenceStatus.MATCHED for status in present):
        return ReturnReferenceStatus.MATCHED
    for status in OVERALL_STATUS_PRECEDENCE:
        if status in present:
            return status
    return ReturnReferenceStatus.MISMATCH


def _page_full(search: SearchOutcome) -> bool:
    # An unknown page size cannot prove the page was short: treat it as full.
    return search.page_size is None or search.returned >= search.page_size


def _same_tax_number(left: str | None, right: str | None) -> bool:
    left_digits, right_digits = _digits(left), _digits(right)
    return bool(left_digits) and left_digits == right_digits


def _same_code(left: str | None, right: str | None) -> bool:
    if left is None or right is None or not left.strip():
        return False
    return left.strip().upper() == right.strip().upper()


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value) if value else ""
