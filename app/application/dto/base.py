from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ApplicationDTO:
    """Base type for immutable application-layer data transfer objects."""
