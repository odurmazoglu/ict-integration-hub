from __future__ import annotations

from typing import Protocol


class UnitOfWork(Protocol):
    """Transaction boundary port for future state-changing use cases."""

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass
