from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.connectors.odoo.client import OdooJson2Client  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.services.odoo_staging_validation import OdooStagingValidationService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate read-only Odoo staging connectivity.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print the sanitized JSON report.")
    args = parser.parse_args()

    report = asyncio.run(_run())
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if report["overall_status"] == "ok" else 1


async def _run() -> dict[str, Any]:
    settings = get_settings()
    client = OdooJson2Client.from_settings(settings)
    return await OdooStagingValidationService(settings=settings, client=client).validate()


if __name__ == "__main__":
    raise SystemExit(main())
