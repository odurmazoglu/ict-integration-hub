"""Controlled, idempotent onboarding of one supplier-level operating-expense mapping.

The operator supplies the approved Odoo ``expense_account_id`` explicitly; it is never
inferred, and this script never contacts Odoo. An identical mapping is a no-op; a divergent
one fails closed. Run against the Hub database only.

Example:
    python -m scripts.onboard_operating_expense_mapping \\
        --company-id 1 --vendor-partner-id 101 \\
        --expense-account-id 9001 --expense-category OFFICE_OPERATING_EXPENSE --confirm
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.application.expense_mapping import (  # noqa: E402
    OnboardOperatingExpenseMappingCommand,
    OnboardOperatingExpenseMappingUseCase,
    OperatingExpenseMappingError,
)
from app.db.session import SessionLocal  # noqa: E402
from app.persistence import SqlAlchemyOperatingExpenseMappingRepository  # noqa: E402


def main() -> int:
    args = _parse_args()
    if not args.confirm:
        print(json.dumps({"status": "refused", "reason": "--confirm is required for a mapping write."}))
        return 2

    command = OnboardOperatingExpenseMappingCommand(
        company_id=args.company_id,
        vendor_partner_id=args.vendor_partner_id,
        expense_account_id=args.expense_account_id,
        expense_category=args.expense_category,
        enabled=not args.disabled,
    )
    try:
        with SessionLocal() as session:
            use_case = OnboardOperatingExpenseMappingUseCase(SqlAlchemyOperatingExpenseMappingRepository(session))
            result = use_case.execute(command)
            session.commit()
    except OperatingExpenseMappingError as exc:
        print(json.dumps({"status": "failed", "reason": exc.safe_message}))
        return 1

    print(
        json.dumps(
            {
                "status": "ok",
                "outcome": result.outcome.value,
                "mapping": {
                    "id": result.mapping.id,
                    "company_id": result.mapping.company_id,
                    "vendor_partner_id": result.mapping.vendor_partner_id,
                    "expense_account_id": result.mapping.expense_account_id,
                    "expense_category": result.mapping.expense_category,
                    "enabled": result.mapping.enabled,
                },
            }
        )
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Onboard one operating-expense mapping (Hub DB only).")
    parser.add_argument("--company-id", type=int, required=True, help="Odoo company id.")
    parser.add_argument("--vendor-partner-id", type=int, required=True, help="Resolved Odoo res.partner id.")
    parser.add_argument(
        "--expense-account-id", type=int, required=True, help="Approved Odoo account id (never inferred)."
    )
    parser.add_argument(
        "--expense-category", required=True, help="Stable uppercase code, e.g. OFFICE_OPERATING_EXPENSE."
    )
    parser.add_argument("--disabled", action="store_true", help="Persist as a disabled historical row.")
    parser.add_argument("--confirm", action="store_true", help="Required. Acknowledges a Hub database write.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
