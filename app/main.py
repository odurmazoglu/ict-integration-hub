from fastapi import FastAPI

from app.api.error_handling import install_api_exception_handlers
from app.api.routers import document_download, health, odoo, odoo_mapping, uyumsoft, uyumsoft_sync, workbench
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.runtime_checks import validate_runtime_configuration


def create_app() -> FastAPI:
    settings = get_settings()
    validate_runtime_configuration(settings)
    configure_logging(settings)

    app = FastAPI(title="ICT Integration Hub", version="0.1.0")
    install_api_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(odoo.router)
    app.include_router(odoo_mapping.router)
    app.include_router(uyumsoft.router)
    app.include_router(uyumsoft_sync.router)
    app.include_router(document_download.router)
    app.include_router(workbench.router)
    return app


app = create_app()
