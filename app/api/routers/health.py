from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.api.dependencies import SettingsDep
from app.core.runtime_checks import configuration_check, database_check, document_storage_check

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/live")
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
def readiness(settings: SettingsDep) -> dict[str, Any]:
    checks = [
        configuration_check(settings),
        database_check(settings.database_url),
        document_storage_check(settings.document_storage_root),
    ]
    payload = {
        "status": "ready" if all(check.status == "ok" for check in checks) else "not_ready",
        "checks": [{"name": check.name, "status": check.status, "message": check.message} for check in checks],
    }
    if payload["status"] != "ready":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=payload)
    return payload
