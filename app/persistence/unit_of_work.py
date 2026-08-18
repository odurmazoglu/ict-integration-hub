from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class SqlAlchemyUnitOfWork:
    """SQLAlchemy transaction boundary for application use cases."""

    session: Session

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()
