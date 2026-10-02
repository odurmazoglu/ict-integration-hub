"""Read-only operator access to historical ONE_OFF_VENDOR retirement rows.

The archive-after-Vendor-Bill lifecycle (and its explicit recovery workflow) is
retired: one-off supplier partners stay active. Historical rows are audit evidence
and stay readable here; nothing can advance or act on them.
"""

from app.application.workbench.exceptions import ReviewNotFoundError
from app.application.workbench.one_off_vendor_retirement import OneOffVendorRetirement
from app.application.workbench.ports import OneOffVendorRetirementWriter, ReviewQueueReader
from app.application.workbench.queries import ReviewDetailQuery


class GetOneOffVendorRetirementUseCase:
    def __init__(self, *, review_reader: ReviewQueueReader, retirement_reader: OneOffVendorRetirementWriter) -> None:
        self._review_reader = review_reader
        self._retirement_reader = retirement_reader

    def execute(self, *, review_id: str, company_id: int, review_version: int | None = None) -> OneOffVendorRetirement:
        self._review_reader.get_review_item(ReviewDetailQuery(review_id=review_id, company_id=company_id))
        if review_version is None:
            retirement = self._retirement_reader.find_latest_for_review(review_id=review_id, company_id=company_id)
        else:
            retirement = self._retirement_reader.find(
                review_id=review_id, company_id=company_id, review_version=review_version
            )
        if retirement is None:
            raise ReviewNotFoundError("One-off vendor retirement was not found in this review scope.")
        return retirement
