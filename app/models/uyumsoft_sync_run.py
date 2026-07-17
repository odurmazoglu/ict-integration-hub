from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import AwareDateTime


class UyumsoftSyncRun(Base):
    __tablename__ = "uyumsoft_sync_runs"
    __table_args__ = (
        Index("ix_uyumsoft_sync_runs_provider_status", "provider", "status"),
        Index("ix_uyumsoft_sync_runs_window", "from_date", "to_date"),
        Index("ix_uyumsoft_sync_runs_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_directions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    from_date: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    to_date: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    page_size: Mapped[int] = mapped_column(Integer, nullable=False)
    max_pages: Mapped[int] = mapped_column(Integer, nullable=False)
    current_direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    current_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pages_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invoices_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cursor_state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    failure_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    failure_detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
