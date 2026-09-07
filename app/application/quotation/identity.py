from __future__ import annotations

import hashlib
import json

from app.application.quotation.contracts import CreateQuotationScenarioCommand

CUSTOMER_QUOTATION_EXECUTION_TYPE = "customer_quotation_creation"


def customer_quotation_execution_key(
    *,
    company_id: int,
    review_id: str,
    decision_id: str,
    decision_version: int,
    scenario_id: str,
) -> str:
    """Return the stable technical execution key for one accepted scenario quotation.

    The key is a deterministic function of the immutable scenario evidence
    identity only. It excludes timestamps, runtime UUIDs, ``sale.order`` ids,
    monetary values, and mutable labels, so the same scenario identity always
    maps to the same key.
    """

    identity = {
        "execution_type": CUSTOMER_QUOTATION_EXECUTION_TYPE,
        "company_id": company_id,
        "review_id": review_id,
        "decision_id": decision_id,
        "decision_version": decision_version,
        "scenario_id": scenario_id,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"quotation-scenario-execution:{digest}"


def quotation_scenario_execution_key(command: CreateQuotationScenarioCommand) -> str:
    """Return a stable identity for one accepted scenario quotation execution."""

    return customer_quotation_execution_key(
        company_id=command.company_id,
        review_id=command.review_id,
        decision_id=command.decision_id,
        decision_version=command.decision_version,
        scenario_id=command.scenario.scenario_id,
    )
