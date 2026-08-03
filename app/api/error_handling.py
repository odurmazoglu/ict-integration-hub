from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.security import (
    AuthenticationRequiredError,
    DevelopmentAuthenticationDisabledError,
    InvalidAuthenticationContextError,
    InvalidTokenError,
    OidcProviderUnavailableError,
    PermissionDeniedError,
    SecurityContextError,
)
from app.api.security.trace import HEADER_TRACE_ID, parse_trace_id
from app.schemas.workbench import ApiErrorItem, ErrorEnvelope


def install_api_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(SecurityContextError, _security_context_exception_handler)
    app.add_exception_handler(RequestValidationError, _request_validation_exception_handler)


async def _request_validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError) or not request.url.path.startswith("/api/workbench"):
        return await request_validation_exception_handler(request, exc)
    return _error_response(
        status_code=HTTPStatus.BAD_REQUEST,
        code="request_validation_error",
        message="Request validation failed.",
        trace_id=_trace_id_from_request(request),
    )


async def _security_context_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, SecurityContextError):
        return _error_response(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            code="internal_error",
            message="Internal server error.",
            trace_id=_trace_id_from_request(request),
        )
    return _error_response(
        status_code=_security_status_code(exc),
        code=exc.error_category,
        message=exc.safe_message,
        trace_id=_trace_id_from_request(request),
    )


def _security_status_code(exc: SecurityContextError) -> int:
    if isinstance(exc, PermissionDeniedError):
        return HTTPStatus.FORBIDDEN
    if isinstance(exc, OidcProviderUnavailableError):
        return HTTPStatus.SERVICE_UNAVAILABLE
    if isinstance(
        exc,
        (
            AuthenticationRequiredError,
            DevelopmentAuthenticationDisabledError,
            InvalidAuthenticationContextError,
            InvalidTokenError,
        ),
    ):
        return HTTPStatus.UNAUTHORIZED
    return HTTPStatus.UNAUTHORIZED


def _trace_id_from_request(request: Request) -> str:
    try:
        return parse_trace_id(request.headers.get(HEADER_TRACE_ID))
    except InvalidAuthenticationContextError:
        return str(uuid4())


def error_response_factory(trace_id: str) -> Callable[[int, str, str], JSONResponse]:
    def build(status_code: int, code: str, message: str) -> JSONResponse:
        return _error_response(status_code=status_code, code=code, message=message, trace_id=trace_id)

    return build


def _error_response(*, status_code: int, code: str, message: str, trace_id: str) -> JSONResponse:
    envelope = ErrorEnvelope(
        success=False,
        data=None,
        warnings=[],
        errors=[ApiErrorItem(code=code, message=message)],
        trace_id=trace_id,
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers={"X-Trace-ID": trace_id},
    )
