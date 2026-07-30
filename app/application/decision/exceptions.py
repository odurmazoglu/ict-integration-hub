from __future__ import annotations

from app.application.exceptions import ApplicationError


class UnsupportedWorkflowError(ApplicationError):
    error_category = "unsupported_workflow"
