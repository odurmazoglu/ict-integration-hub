from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OneOffVendorArchiveWriteStatus(StrEnum):
    #: The write happened this call: res.partner.active was True, is now False.
    ARCHIVED = "archived"
    #: Read-back-first found the partner already inactive; no write was made.
    ALREADY_ARCHIVED = "already_archived"


@dataclass(frozen=True, slots=True)
class OneOffVendorArchiveWriteResult:
    status: OneOffVendorArchiveWriteStatus
    partner_id: int
    safe_message: str | None = None
