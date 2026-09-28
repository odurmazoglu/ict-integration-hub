"""Dependency-light contracts for Workbench projection sync (OPS-UI-01A).

Kept separate from :mod:`app.application.workbench.projection_sync` so decision,
remediation and execution use cases can depend on the post-commit hook without
importing execution/evidence modules (which themselves import this package).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from app.application.dto import ApplicationDTO

if TYPE_CHECKING:
    from app.application.workbench.projection import WorkbenchProjection

PROJECTION_SYNC_WARNING = (
    "Odoo Workbench projection was not updated; the Hub state is committed and authoritative. "
    "Run the Workbench projection reconcile to converge."
)


class ProjectionSyncOutcome(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    NO_CHANGE = "no_change"
    #: The Odoo row already reflects a newer Hub state; this (older) snapshot is not written.
    SKIPPED_STALE = "skipped_stale"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ProjectionFieldChange(ApplicationDTO):
    field: str
    before: Any
    after: Any


@dataclass(frozen=True, slots=True)
class ProjectionSyncResult(ApplicationDTO):
    """Outcome of projecting one review. ``applied`` is False for a dry-run plan."""

    review_id: str
    outcome: ProjectionSyncOutcome
    applied: bool
    odoo_record_id: int | None = None
    review_version: int | None = None
    changes: tuple[ProjectionFieldChange, ...] = field(default_factory=tuple)
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.outcome is ProjectionSyncOutcome.ERROR


class WorkbenchProjectionSyncPublisher(Protocol):
    """ERP adapter that diffs one full snapshot against the Odoo row and (optionally) writes it."""

    def sync_projection(self, projection: WorkbenchProjection, *, apply: bool) -> ProjectionSyncResult:
        pass


class ReviewProjectionSynchronizer(Protocol):
    """What projection-relevant use cases depend on: one post-commit call per transition."""

    def sync(self, *, review_id: str, company_id: int) -> ProjectionSyncResult:
        pass


def sync_after_commit(
    synchronizer: ReviewProjectionSynchronizer | None, *, review_id: str, company_id: int
) -> ProjectionSyncResult | None:
    """Post-commit hook used by every projection-relevant use case.

    ``None`` means projection publishing is disabled: nothing is read or written.
    Call it only *after* the business transition's Hub commit.
    """

    if synchronizer is None:
        return None
    return synchronizer.sync(review_id=review_id, company_id=company_id)


def projection_sync_warnings(result: ProjectionSyncResult | None) -> tuple[str, ...]:
    """The visible warning a use case attaches to its (already committed) result."""

    if result is None or not result.failed:
        return ()
    return (PROJECTION_SYNC_WARNING,)


__all__ = [
    "PROJECTION_SYNC_WARNING",
    "ProjectionFieldChange",
    "ProjectionSyncOutcome",
    "ProjectionSyncResult",
    "ReviewProjectionSynchronizer",
    "WorkbenchProjectionSyncPublisher",
    "projection_sync_warnings",
    "sync_after_commit",
]
