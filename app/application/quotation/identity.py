from __future__ import annotations

import hashlib
import json

from app.application.quotation.contracts import CreateQuotationScenarioCommand


def quotation_scenario_execution_key(command: CreateQuotationScenarioCommand) -> str:
    """Return a stable identity for one accepted scenario quotation execution."""

    identity = {
        "execution_type": "customer_quotation_creation",
        "company_id": command.company_id,
        "review_id": command.review_id,
        "decision_id": command.decision_id,
        "decision_version": command.decision_version,
        "scenario_id": command.scenario.scenario_id,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"quotation-scenario-execution:{digest}"
