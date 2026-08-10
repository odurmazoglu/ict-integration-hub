from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from app.application.execution.contracts import ExecutionApproval, ExecutionMode, ExecutionPlan, ExecutionStepType
from app.application.execution.exceptions import (
    ExecutionApprovalError,
    ExecutionModeNotEnabledError,
    ExecutionUnsupportedStepError,
)


class RealWriteGate(Protocol):
    """Approval gate owned by the concrete writer policy."""

    def ensure_real_write_allowed(self, *, approved_by: str | None) -> None:
        pass


@dataclass(frozen=True, slots=True)
class ExecutionPreflightPolicy:
    """Pure preflight guard that runs before durable runtime mutation."""

    production_execution_enabled: bool = False
    real_write_gate: RealWriteGate | None = None
    real_write_gates: Mapping[ExecutionStepType, RealWriteGate] | None = None
    writer_step_types: tuple[ExecutionStepType, ...] = (ExecutionStepType.VENDOR_BILL,)

    def ensure_execute_allowed(self, *, plan: ExecutionPlan, approval: ExecutionApproval | None) -> None:
        if plan.mode is not ExecutionMode.EXECUTE:
            return
        if approval is None:
            raise ExecutionApprovalError("Explicit execution approval is required for EXECUTE mode.")
        if not self.production_execution_enabled:
            raise ExecutionModeNotEnabledError("Production execution must be explicitly enabled.")
        for step in plan.steps:
            if not step.execute_supported:
                raise ExecutionUnsupportedStepError("Execution plan contains a step that is not execute-capable.")
        called_gate_ids: set[int] = set()
        for step in plan.steps:
            if not step.writer_required and step.step_type not in self.writer_step_types:
                continue
            gate = self._gate_for(step.step_type)
            if gate is None or id(gate) in called_gate_ids:
                continue
            gate.ensure_real_write_allowed(approved_by=approval.approved_by)
            called_gate_ids.add(id(gate))

    def _gate_for(self, step_type: ExecutionStepType) -> RealWriteGate | None:
        if self.real_write_gates is not None:
            return self.real_write_gates.get(step_type)
        return self.real_write_gate
