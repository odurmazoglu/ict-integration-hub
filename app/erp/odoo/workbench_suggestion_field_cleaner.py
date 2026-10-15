"""PR F / F3.6b: find and clear the two advisory suggestion fields on Workbench child rows.

This is the only Odoo access of ``clear-display``. It reads ``x_ipp_wb_product_line`` rows of one
company whose suggested-products Many2many or suggestion summary is populated (current and
non-current rows alike), and writes exactly those two fields back to empty. The write payload is
built from the two mapped field names only and checked before every call, so a product, an
operator input (``req_*`` / ``new_*``), a parent field, supplierinfo or accounting can never be
written from here. The summary text itself is never kept: a row carries only its length and a
short digest, so an audit report holds no invoice content.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.application.workbench.exceptions import WorkbenchContractError
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.workbench_product_line_publisher import OPERATOR_INPUT_PREFIXES, OdooWorkbenchProductLineFieldMapping
from app.erp.odoo.workbench_projection_publisher import OdooWorkbenchProjectionAdapter

#: Rows per ``search_read`` page and the hard bound on discovered rows (fail closed above it).
PAGE_SIZE = 200
MAX_POPULATED_ROWS = 5000
_DIGEST_LENGTH = 12


@dataclass(frozen=True, slots=True)
class PopulatedSuggestionRow:
    """A child row that still shows a suggestion (no summary text, only its length and digest)."""

    record_id: int
    review_id: str | None
    line_key: str | None
    is_current: bool
    product_ids: tuple[int, ...]
    summary_length: int
    summary_digest: str | None


class OdooWorkbenchSuggestionFieldCleaner:
    """Read populated suggestion fields and clear them; nothing else is ever written."""

    def __init__(
        self,
        *,
        adapter: OdooWorkbenchProjectionAdapter,
        mapping: OdooWorkbenchProductLineFieldMapping,
        page_size: int = PAGE_SIZE,
        max_rows: int = MAX_POPULATED_ROWS,
    ) -> None:
        if not mapping.suggestions_mapped:
            raise WorkbenchContractError(
                "clear-display requires ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTED_PRODUCTS_FIELD and "
                "ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTION_SUMMARY_FIELD."
            )
        self._adapter = adapter
        self._mapping = mapping
        self._page_size = page_size
        self._max_rows = max_rows
        self._fields = (str(mapping.suggested_products), str(mapping.suggestion_summary))

    @property
    def cleared_fields(self) -> tuple[str, str]:
        return self._fields

    def populated_rows(self, *, company_id: int) -> tuple[PopulatedSuggestionRow, ...]:
        """Every row of ``company_id`` with either field populated, ordered by record id.

        One bounded, paginated query per field (no OR domain), merged by id. More rows than
        ``max_rows`` raise instead of returning a partial answer.
        """

        found: dict[int, dict[str, Any]] = {}
        for field_name in self._fields:
            for record in self._pages(company_id=company_id, field_name=field_name):
                found[_record_id(record)] = record
                if len(found) > self._max_rows:
                    raise ErpRepositoryError("Too many Workbench rows with suggestion fields; nothing was cleared.")
        return tuple(self._row(found[record_id]) for record_id in sorted(found))

    def clear(self, row: PopulatedSuggestionRow) -> None:
        values = {self._fields[0]: [[6, 0, []]], self._fields[1]: False}
        self._check_payload(values)
        self._adapter.write(model=self._mapping.model, record_id=row.record_id, values=values)

    # ------------------------------------------------------------------ internals

    def _pages(self, *, company_id: int, field_name: str) -> list[dict[str, Any]]:
        mapping = self._mapping
        records: list[dict[str, Any]] = []
        seen: set[int] = set()
        offset = 0
        while True:
            page = self._adapter.search_read(
                model=mapping.model,
                domain=[[mapping.company_id, "=", company_id], [field_name, "!=", False]],
                fields=["id", mapping.review_id, mapping.line_key, mapping.is_current, *self._fields],
                limit=self._page_size,
                offset=offset,
            )
            fresh = [record for record in page if _record_id(record) not in seen]
            seen.update(_record_id(record) for record in fresh)
            records.extend(fresh)
            if len(records) > self._max_rows:
                raise ErpRepositoryError("Too many Workbench rows with suggestion fields; nothing was cleared.")
            if len(page) < self._page_size or not fresh:
                return records
            offset += len(page)

    def _row(self, record: dict[str, Any]) -> PopulatedSuggestionRow:
        mapping = self._mapping
        summary = record.get(mapping.suggestion_summary)
        text = summary if isinstance(summary, str) and summary else None
        return PopulatedSuggestionRow(
            record_id=_record_id(record),
            review_id=_text(record.get(mapping.review_id)),
            line_key=_text(record.get(mapping.line_key)),
            is_current=bool(record.get(mapping.is_current)),
            product_ids=tuple(sorted(_ids(record.get(mapping.suggested_products)))),
            summary_length=len(text) if text else 0,
            summary_digest=hashlib.sha256(text.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH] if text else None,
        )

    def _check_payload(self, values: dict[str, Any]) -> None:
        names = set(values)
        if names != set(self._fields):
            raise WorkbenchContractError("clear-display may write only the two suggestion fields.")
        mapping = self._mapping
        forbidden = {mapping.product, mapping.match_state, mapping.parent, mapping.is_current}
        if names & forbidden or any(name.startswith(OPERATOR_INPUT_PREFIXES) for name in names):
            raise WorkbenchContractError("clear-display may never write a product, state or operator input field.")


def _record_id(record: dict[str, Any]) -> int:
    value = record.get("id")
    if type(value) is not int or value <= 0:
        raise ErpRepositoryError("Odoo returned a Workbench row without a valid id.")
    return value


def _ids(value: Any) -> list[int]:
    if not isinstance(value, list | tuple):
        return []
    return [item for item in value if type(item) is int]


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


__all__ = [
    "MAX_POPULATED_ROWS",
    "PAGE_SIZE",
    "OdooWorkbenchSuggestionFieldCleaner",
    "PopulatedSuggestionRow",
]
