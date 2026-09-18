"""Operator reads and explicit recovery of existing retirement lifecycle rows."""

from collections.abc import Callable

from app.application.workbench.exceptions import ReviewNotFoundError
from app.application.workbench.one_off_vendor_retirement import (
    ArchiveOneOffVendorCommand,
    ArchiveOneOffVendorResult,
    OneOffVendorRetirement,
)
from app.application.workbench.one_off_vendor_use_cases import ArchiveOneOffVendorUseCase
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


class RecoverOneOffVendorRetirementWorkflow:
    def __init__(
        self,
        *,
        status_reader: GetOneOffVendorRetirementUseCase,
        archive_use_case_factory: Callable[..., ArchiveOneOffVendorUseCase],
    ) -> None:
        self._status_reader = status_reader
        self._archive_use_case_factory = archive_use_case_factory

    async def execute(
        self,
        command: ArchiveOneOffVendorCommand,
        *,
        approved_by: str,
        authorization_id: str | None = None,
    ) -> ArchiveOneOffVendorResult:
        # Validate persisted exact tenant/version before constructing any ERP adapter.
        self._status_reader.execute(
            review_id=command.review_id, company_id=command.company_id, review_version=command.review_version
        )
        use_case = self._archive_use_case_factory(approved_by=approved_by, authorization_id=authorization_id)
        return await use_case.execute(command)
