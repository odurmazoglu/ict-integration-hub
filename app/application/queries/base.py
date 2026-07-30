from __future__ import annotations

from dataclasses import dataclass

from app.application.dto import ApplicationDTO


@dataclass(frozen=True, slots=True)
class Query(ApplicationDTO):
    """Base type for read-only application requests."""
