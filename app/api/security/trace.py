from __future__ import annotations

import re
from uuid import uuid4

from app.api.security.exceptions import InvalidAuthenticationContextError

HEADER_TRACE_ID = "x-trace-id"
MAX_TRACE_ID_LENGTH = 128
TRACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")


def parse_trace_id(value: str | None) -> str:
    if value is None or not value.strip():
        return str(uuid4())
    trace_id = value.strip()
    if len(trace_id) > MAX_TRACE_ID_LENGTH or TRACE_ID_PATTERN.fullmatch(trace_id) is None:
        raise InvalidAuthenticationContextError("Trace authentication context is invalid.")
    return trace_id
