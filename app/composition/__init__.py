"""Production composition roots for application use cases."""

from app.composition.execution import (
    build_vendor_bill_execution_use_case,
    build_workbench_vendor_bill_execution_workflow,
)
from app.composition.imports import (
    build_import_invoice_use_case,
    build_odoo_workbench_decision_ingestion_workflow,
    build_odoo_workbench_projection_publisher,
    build_uyumsoft_canonical_invoice_importer,
)

__all__ = [
    "build_import_invoice_use_case",
    "build_odoo_workbench_decision_ingestion_workflow",
    "build_odoo_workbench_projection_publisher",
    "build_uyumsoft_canonical_invoice_importer",
    "build_vendor_bill_execution_use_case",
    "build_workbench_vendor_bill_execution_workflow",
]
