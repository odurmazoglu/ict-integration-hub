"""PR F / F3.1: read-only advisory product hints shown on one projected Workbench line.

A view is display data only. It is produced outside the Workbench contracts, from
persisted advisory evidence that already passed the display gate (pending review,
current version, current PRODUCT_NOT_FOUND line, same line identity, effective supplier
and seller code, validated within the projection cutoff). The projection never derives,
ranks or checks a hint itself, and nothing in the Workbench reads a view back as input:
it never selects, maps or creates a product and never changes the line's match state.

A view may carry no product at all (notes-only): the summary then holds only advisory
warnings, e.g. a supplier code that differs only in letter case or spacing.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import WorkbenchContractError

#: The advisory engine returns at most three ranked candidates per line.
MAX_HINT_PRODUCTS = 3


@dataclass(frozen=True, slots=True)
class ProductLineSuggestionView(ApplicationDTO):
    """Ranked candidate product ids (possibly none) plus a plain-text summary for one child line row."""

    line_key: str
    product_ids: tuple[int, ...]
    summary: str

    def __post_init__(self) -> None:
        if not isinstance(self.line_key, str) or not self.line_key.strip():
            raise WorkbenchContractError("A product line hint needs its line key.")
        ids = tuple(self.product_ids)
        if len(ids) > MAX_HINT_PRODUCTS:
            raise WorkbenchContractError("A product line hint carries at most three products.")
        if any(type(item) is not int or item <= 0 for item in ids) or len(set(ids)) != len(ids):
            raise WorkbenchContractError("Product line hint ids must be distinct positive ids.")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise WorkbenchContractError("A product line hint needs its summary text.")
        object.__setattr__(self, "product_ids", ids)


__all__ = ["MAX_HINT_PRODUCTS", "ProductLineSuggestionView"]
