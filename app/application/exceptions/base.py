from __future__ import annotations


class ApplicationError(Exception):
    """Base exception for use-case orchestration failures."""

    error_category = "application_error"

    def __init__(self, safe_message: str) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
