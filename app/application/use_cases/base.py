from __future__ import annotations

from typing import Protocol, TypeVar

RequestT = TypeVar("RequestT")
ResultT = TypeVar("ResultT")


class UseCase(Protocol[RequestT, ResultT]):
    """Executable application workflow boundary."""

    def execute(self, request: RequestT) -> ResultT:
        pass
