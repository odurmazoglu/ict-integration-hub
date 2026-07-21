from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class VendorBillValidationResult:
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_valid(self) -> bool:
        return not self.errors


def validation_result(errors: list[str]) -> VendorBillValidationResult:
    return VendorBillValidationResult(errors=tuple(errors))
