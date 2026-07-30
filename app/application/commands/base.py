from __future__ import annotations

from dataclasses import dataclass

from app.application.dto import ApplicationDTO


@dataclass(frozen=True, slots=True)
class Command(ApplicationDTO):
    """Base type for state-changing application requests."""
