from __future__ import annotations

from pathlib import Path

import pytest

from app.application.dto import DecisionResult
from app.application.use_cases import ImportInvoiceUseCase
from app.application.workbench import ReviewItemCreationService
from app.application.workbench.classification_projection import WorkbenchClassificationProjectionService
from app.application.workflow import WorkflowType
from app.composition import build_import_invoice_use_case, build_uyumsoft_canonical_invoice_importer
from app.core.config import Settings
from app.erp.odoo import OdooWorkbenchProjectionPublisher
from app.persistence import SqlAlchemyUnitOfWork


def test_production_import_composition_omits_odoo_publisher_when_flag_is_false() -> None:
    use_case = build_import_invoice_use_case(
        import_history=FakeImportHistory(),
        decision_engine=FakeDecisionEngine(),
        session=FakeSession(),
        settings=Settings(odoo_workbench_projection_publish_enabled=False),
    )

    assert isinstance(use_case, ImportInvoiceUseCase)
    assert isinstance(use_case._review_item_creation_service, ReviewItemCreationService)
    assert isinstance(use_case._unit_of_work, SqlAlchemyUnitOfWork)
    assert use_case._workbench_projection_publisher is None


def test_production_import_composition_wires_odoo_publisher_when_flag_is_true(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_projection_mapping(monkeypatch)

    use_case = build_import_invoice_use_case(
        import_history=FakeImportHistory(),
        decision_engine=FakeDecisionEngine(),
        session=FakeSession(),
        settings=Settings(odoo_workbench_projection_publish_enabled=True),
        odoo_client=FakeOdooClient(),
    )

    publisher = use_case._workbench_projection_publisher
    assert isinstance(publisher, OdooWorkbenchProjectionPublisher)
    assert isinstance(publisher._classification_service, WorkbenchClassificationProjectionService)


def test_app_has_no_unwired_import_invoice_use_case_construction_paths() -> None:
    construction_sites: list[str] = []
    for path in Path("app").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text()
        if "ImportInvoiceUseCase(" in source:
            construction_sites.append(path.as_posix())

    assert construction_sites == ["app/composition/imports.py"]


def test_uyumsoft_sync_route_uses_canonical_importer_dependency() -> None:
    route_source = Path("app/api/routers/uyumsoft_sync.py").read_text()
    dependency_source = Path("app/api/dependencies.py").read_text()

    assert "UyumsoftCanonicalImporterDep" in route_source
    assert "canonical_importer=canonical_importer" in route_source
    assert "build_uyumsoft_canonical_invoice_importer" in dependency_source


def test_docs_state_uyumsoft_external_import_trigger_is_live() -> None:
    docs_paths = [
        Path("docs/IMPORT_WORKBENCH.md"),
        Path("docs/APPLICATION_LAYER.md"),
        Path("docs/ODOO_WORKBENCH_PROJECTION.md"),
    ]
    if not all(path.exists() for path in docs_paths):
        pytest.skip("Documentation files are not copied into the runtime Docker image.")

    import_workbench = docs_paths[0].read_text()
    application_layer = docs_paths[1].read_text()
    projection = docs_paths[2].read_text()
    readme = Path("README.md").read_text()

    assert "Uyumsoft inbound sync endpoint" in import_workbench
    assert "build_import_invoice_use_case(...)" in import_workbench
    assert "ImportInvoiceUseCase.execute(...)" in application_layer
    assert "live Uyumsoft inbound import attachment" in projection
    assert "Uyumsoft inbound sync now continues through the canonical import pipeline" in readme


def test_uyumsoft_importer_composition_preserves_workbench_feature_flag_false() -> None:
    importer = build_uyumsoft_canonical_invoice_importer(
        session=FakeSession(),
        settings=Settings(odoo_workbench_projection_publish_enabled=False),
        uyumsoft_client=FakeUyumsoftClient(),
        storage=FakeStorage(),
        odoo_client=FakeOdooClient(),
    )

    use_case = importer._import_use_case_factory()  # noqa: SLF001

    assert isinstance(use_case, ImportInvoiceUseCase)
    assert use_case._workbench_projection_publisher is None


def test_uyumsoft_importer_composition_preserves_workbench_feature_flag_true(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_projection_mapping(monkeypatch)
    importer = build_uyumsoft_canonical_invoice_importer(
        session=FakeSession(),
        settings=Settings(odoo_workbench_projection_publish_enabled=True),
        uyumsoft_client=FakeUyumsoftClient(),
        storage=FakeStorage(),
        odoo_client=FakeOdooClient(),
    )

    use_case = importer._import_use_case_factory()  # noqa: SLF001

    assert isinstance(use_case._workbench_projection_publisher, OdooWorkbenchProjectionPublisher)


class FakeSession:
    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class FakeImportHistory:
    def find_imported_invoice(self, idempotency_key: str) -> None:
        return None


class FakeDecisionEngine:
    async def decide(self, command: object) -> DecisionResult:
        return DecisionResult(
            success=True,
            invoice_id="INV-ETTN",
            workflow=WorkflowType.VENDOR_BILL,
            strategy=WorkflowType.VENDOR_BILL.value,
            status="dry_run",
        )


class FakeOdooClient:
    pass


class FakeUyumsoftClient:
    pass


class FakeStorage:
    backend_name = "fake"

    def read(self, storage_key: str) -> bytes:
        return b""

    def write(self, storage_key: str, content: bytes) -> None:
        pass

    def delete(self, storage_key: str) -> None:
        pass


def _configure_projection_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "PARENT_MODEL": "x_ipp_import_review",
        "REVIEW_ID_FIELD": "x_review_id",
        "COMPANY_ID_FIELD": "x_company_id",
        "INVOICE_NUMBER_FIELD": "x_invoice_number",
        "SUPPLIER_FIELD": "x_supplier",
        "SUPPLIER_TAX_NUMBER_FIELD": "x_supplier_tax_number",
        "INVOICE_DATE_FIELD": "x_invoice_date",
        "CURRENCY_FIELD": "x_currency",
        "INVOICE_TOTAL_FIELD": "x_invoice_total",
        "REVIEW_STATUS_FIELD": "x_review_status",
        "WORKFLOW_FIELD": "x_workflow",
        "REVIEW_VERSION_FIELD": "x_review_version",
        "LAST_SYNC_AT_FIELD": "x_last_sync_at",
    }
    for key, value in values.items():
        monkeypatch.setenv(f"ODOO_WORKBENCH_PUBLISHER_{key}", value)
