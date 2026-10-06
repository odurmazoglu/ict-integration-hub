"""Operator guidance projected to the Odoo Workbench (ADR-0013).

Answers, from committed Hub state only: what is preventing this review from
proceeding, what the operator must do next, and what is already done. It is a
*presentation* of existing facts -- review status, current blocker codes, the purchase
purpose recorded for the current version, the accounting resolution, the accepted
decision and the stored execution. The step order mirrors the existing use-case
preconditions; it never decides eligibility (the use cases re-validate every request)
and never changes backend reason codes (they stay visible under "Teknik / Denetim").
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from enum import StrEnum

from app.application.dto import ApplicationDTO
from app.application.exceptions.base import ApplicationError
from app.application.workbench.accounting_resolution import AccountingTreatmentType, ReviewAccountingResolution
from app.application.workbench.dto import ReviewDecisionType, ReviewStatus
from app.application.workbench.fixed_asset_lookup import FixedAssetAccountingReader
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workflow import ManualReviewReasonCode, WorkflowType

logger = logging.getLogger(__name__)

#: Canonical ``ExecutionState.COMPLETED`` value, as stored on the projection.
_EXECUTION_COMPLETED = "completed"
#: Shown instead of a label Odoo could not provide right now; raw ids stay in the
#: technical view (the decision-basis line resolutions), never in the operator summary.
ACCOUNT_LABEL_UNAVAILABLE = "hesap bilgisi şu an okunamadı"
MODEL_LABEL_UNAVAILABLE = "amortisman modeli bilgisi şu an okunamadı"
TREATMENT_LABELS: dict[AccountingTreatmentType, str] = {
    AccountingTreatmentType.EXPENSE_ACCOUNT: "Gider",
    AccountingTreatmentType.CAPITALIZE_FIXED_ASSET: "Sabit Kıymet / Demirbaş",
}


class OperatorNextAction(StrEnum):
    """The single step the operator should take now. Values are the Studio selection labels."""

    SUPPLIER = "Tedarikçi Doğrulanmalı"
    PURPOSE = "Satın Alma Amacı Seçilmeli"
    ACCOUNTING = "Muhasebe İşlemi Seçilmeli"
    DECISION = "Karar Verilmeli"
    EXECUTE = "Fatura Oluşturulmalı"
    TECHNICAL = "Teknik Destek Gerekli"
    DONE = "Tamamlandı"


#: Human-readable titles for backend reason codes. Codes are never changed or hidden
#: from the technical view; this is display text only.
REASON_TITLES: dict[ManualReviewReasonCode, str] = {
    ManualReviewReasonCode.SUPPLIER_TAX_NUMBER_MISSING: "Faturada tedarikçi vergi numarası yok",
    ManualReviewReasonCode.SUPPLIER_NOT_FOUND: "Tedarikçi Odoo'da bulunamadı",
    ManualReviewReasonCode.SUPPLIER_AMBIGUOUS: "Tedarikçi eşleşmesi doğrulanmalı",
    ManualReviewReasonCode.PRODUCT_IDENTIFIER_MISSING: "Fatura satırında ürün kodu yok",
    ManualReviewReasonCode.PRODUCT_NOT_FOUND: "Ürün Odoo'da bulunamadı",
    ManualReviewReasonCode.PRODUCT_AMBIGUOUS: "Ürün eşleşmesi doğrulanmalı",
    ManualReviewReasonCode.PRODUCT_MAPPING_INCOMPLETE: "Ürün eşleşmesi eksik",
    ManualReviewReasonCode.TAX_NOT_FOUND: "Vergi Odoo'da bulunamadı",
    ManualReviewReasonCode.TAX_AMBIGUOUS: "Vergi eşleşmesi doğrulanmalı",
    ManualReviewReasonCode.TAX_MAPPING_INCOMPLETE: "Vergi eşleşmesi eksik",
    ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED: "Muhasebe işlemi seçilmeli",
    ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_AMBIGUOUS: "Muhasebe işlemi netleştirilmeli",
    ManualReviewReasonCode.UNSUPPORTED_INVOICE_CONTENT: "Fatura içeriği desteklenmiyor",
}

PURPOSE_LABELS: dict[PurchasePurpose, str] = {
    PurchasePurpose.INTERNAL_USE: "Şirket İçi Kullanım",
    PurchasePurpose.RESALE: "Yeniden Satış",
    PurchasePurpose.CUSTOMER_PROJECT: "Müşteri Projesi",
    PurchasePurpose.OTHER_OPERATING_EXPENSE: "Diğer İşletme Gideri",
}

WORKFLOW_LABELS: dict[WorkflowType, str] = {
    WorkflowType.VENDOR_BILL: "Tedarikçi Faturası",
    WorkflowType.RFQ: "Teklif İsteği / Satın Alma",
    WorkflowType.EXPENSE: "Gider",
    WorkflowType.ASSET: "Demirbaş",
    WorkflowType.SUBSCRIPTION: "Abonelik / Hizmet",
    WorkflowType.CUSTOMER_QUOTATION: "Müşteri Teklifi",
    WorkflowType.MANUAL_REVIEW: "Manuel İnceleme",
}

_SUPPLIER_CODES = frozenset({ManualReviewReasonCode.SUPPLIER_NOT_FOUND, ManualReviewReasonCode.SUPPLIER_AMBIGUOUS})
_OPERATING_EXPENSE_CODES = frozenset(
    {
        ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED,
        ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_AMBIGUOUS,
    }
)
#: Purposes the accounting-resolution use case supports (it rejects RESALE / CUSTOMER_PROJECT).
_ACCOUNTING_PURPOSES = frozenset({PurchasePurpose.INTERNAL_USE, PurchasePurpose.OTHER_OPERATING_EXPENSE})


@dataclass(frozen=True, slots=True)
class AccountingLabels(ApplicationDTO):
    """Human-readable names of the accounting resolution's Odoo records (``None`` = unavailable)."""

    account: str | None = None
    depreciation_model: str | None = None


def resolve_accounting_labels(
    resolution: ReviewAccountingResolution, reader: FixedAssetAccountingReader
) -> AccountingLabels:
    """Read the selected account / depreciation model names through the existing read-only port.

    Presentation only: an unavailable or missing record degrades to ``None`` (shown as
    "okunamadı"), never to a raw id, and never fails the projection.
    """

    account_id = (
        resolution.asset_account_id
        if resolution.treatment_type is AccountingTreatmentType.CAPITALIZE_FIXED_ASSET
        else resolution.expense_account_id
    )
    account_label: str | None = None
    model_label: str | None = None
    try:
        account = reader.read_account(account_id=account_id) if account_id else None
        if account is not None and account.id == account_id:
            account_label = f"{account.code} — {account.name}" if account.code else account.name
        if (
            resolution.treatment_type is AccountingTreatmentType.CAPITALIZE_FIXED_ASSET
            and resolution.depreciation_model_id
        ):
            model = reader.read_depreciation_model(model_id=resolution.depreciation_model_id)
            if model is not None and model.id == resolution.depreciation_model_id:
                model_label = model.name
    except ApplicationError as exc:
        logger.warning(
            "workbench.operator_guidance.label_unavailable",
            extra={"review_id": resolution.review_id, "error": getattr(exc, "safe_message", None) or str(exc)},
        )
    return AccountingLabels(account=account_label or None, depreciation_model=model_label or None)


@dataclass(frozen=True, slots=True)
class OperatorGuidanceFacts(ApplicationDTO):
    """Committed Hub facts the projection snapshot does not already carry."""

    #: Purpose recorded for the review's *current* version (accounting requires exactly that).
    current_purchase_purpose: PurchasePurpose | None = None
    #: Most recent purpose recorded for the review (for the completed summary).
    latest_purchase_purpose: PurchasePurpose | None = None
    latest_accounting_resolution: ReviewAccountingResolution | None = None
    #: Odoo display names for ``latest_accounting_resolution`` (see ``resolve_accounting_labels``).
    accounting_labels: AccountingLabels | None = None
    #: Configured ``ODOO_FIXED_ASSET_ACCOUNT_IDS`` -- the existing eligibility allowlist.
    eligible_asset_account_ids: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class WorkbenchOperatorGuidance(ApplicationDTO):
    next_action: OperatorNextAction
    todo_html: str
    completed_html: str
    eligible_asset_account_ids: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class GuidanceInput(ApplicationDTO):
    """The projection facts guidance reads (kept narrow to avoid a projection import cycle)."""

    status: ReviewStatus
    reason_codes: tuple[ManualReviewReasonCode, ...]
    supplier_name: str | None
    decision_type: ReviewDecisionType | None
    decision_workflow: WorkflowType | None
    decision_version: int | None
    execution_state: str | None
    vendor_bill_id: int | None


def build_operator_guidance(source: GuidanceInput, facts: OperatorGuidanceFacts) -> WorkbenchOperatorGuidance:
    action, todo = _next_action(source, facts)
    return WorkbenchOperatorGuidance(
        next_action=action,
        todo_html=todo,
        completed_html=_completed_html(source, facts),
        eligible_asset_account_ids=tuple(sorted(set(facts.eligible_asset_account_ids))),
    )


def _next_action(source: GuidanceInput, facts: OperatorGuidanceFacts) -> tuple[OperatorNextAction, str]:
    if source.status in (ReviewStatus.RESOLVED, ReviewStatus.DISMISSED):
        return OperatorNextAction.DONE, _todo("İnceleme kapandı; yapılacak işlem yok.")
    if source.status is ReviewStatus.DECISION_SUBMITTED:
        return _after_decision(source)

    codes = set(source.reason_codes)
    findings = [REASON_TITLES[code] for code in dict.fromkeys(source.reason_codes)]
    if ManualReviewReasonCode.SUPPLIER_NOT_FOUND in codes:
        return OperatorNextAction.SUPPLIER, _todo(
            "Tedarikçiyi doğrulayın: mevcut bir tedarikçiyi seçin veya yeni tedarikçi oluşturun.", findings
        )
    if ManualReviewReasonCode.SUPPLIER_AMBIGUOUS in codes:
        return OperatorNextAction.SUPPLIER, _todo(
            "Birden fazla eşleşen tedarikçi var: doğru mevcut tedarikçiyi seçin.", findings
        )
    if codes & _OPERATING_EXPENSE_CODES:
        purpose = facts.current_purchase_purpose
        if purpose is None:
            return OperatorNextAction.PURPOSE, _todo("Bu alımın amacını seçin.", findings)
        if purpose in _ACCOUNTING_PURPOSES:
            return OperatorNextAction.ACCOUNTING, _todo(
                "Muhasebe işlemini seçin: gider hesabı veya sabit kıymet (demirbaş).", findings
            )
        return OperatorNextAction.TECHNICAL, _todo(
            f"'{PURPOSE_LABELS[purpose]}' amacı için muhasebe adımı henüz Odoo'dan yapılamıyor; teknik destek alın.",
            findings,
        )
    if codes:
        return OperatorNextAction.TECHNICAL, _todo(
            "Bu engel henüz Odoo'dan çözülemiyor (ürün/vergi/fatura içeriği); teknik destek alın.", findings
        )
    return OperatorNextAction.DECISION, _todo("Engel kalmadı: iş akışını seçip kararı gönderin.")


def _after_decision(source: GuidanceInput) -> tuple[OperatorNextAction, str]:
    if source.decision_type is ReviewDecisionType.DISMISS:
        return OperatorNextAction.DONE, _todo("İnceleme reddedildi (karar kaydı mevcut).")
    if source.decision_workflow is WorkflowType.VENDOR_BILL:
        if source.execution_state == _EXECUTION_COMPLETED:
            return OperatorNextAction.DONE, _todo(
                "Taslak tedarikçi faturası oluşturuldu. Faturayı Odoo'da kontrol edip onaylayın."
            )
        return OperatorNextAction.EXECUTE, _todo("Karar kabul edildi: taslak tedarikçi faturasını oluşturun.")
    if source.decision_workflow is WorkflowType.CUSTOMER_QUOTATION:
        return OperatorNextAction.TECHNICAL, _todo("Müşteri teklifi yürütmesi henüz Odoo'dan yapılamıyor.")
    return OperatorNextAction.DONE, _todo("Karar kaydedildi; bu iş akışı için Hub yürütmesi yok.")


def _completed_html(source: GuidanceInput, facts: OperatorGuidanceFacts) -> str:
    done: list[str] = []
    codes = set(source.reason_codes)
    supplier_done = source.status is not ReviewStatus.PENDING_REVIEW or not codes & (
        _SUPPLIER_CODES | {ManualReviewReasonCode.SUPPLIER_TAX_NUMBER_MISSING}
    )
    if supplier_done and source.supplier_name:
        done.append(f"Tedarikçi: {source.supplier_name}")
    if facts.latest_purchase_purpose is not None:
        done.append(f"Satın alma amacı: {PURPOSE_LABELS[facts.latest_purchase_purpose]}")
    resolution = facts.latest_accounting_resolution
    if resolution is not None:
        labels = facts.accounting_labels or AccountingLabels()
        parts = [TREATMENT_LABELS[resolution.treatment_type], labels.account or ACCOUNT_LABEL_UNAVAILABLE]
        if resolution.treatment_type is AccountingTreatmentType.CAPITALIZE_FIXED_ASSET:
            parts.append(labels.depreciation_model or MODEL_LABEL_UNAVAILABLE)
        elif resolution.expense_category:
            parts.append(resolution.expense_category)
        done.append("Muhasebe: " + " — ".join(parts))
    if source.decision_type is ReviewDecisionType.DISMISS:
        done.append(f"Karar: Reddedildi (v{source.decision_version})")
    elif source.decision_workflow is not None:
        done.append(f"Karar: {WORKFLOW_LABELS[source.decision_workflow]} (v{source.decision_version})")
    if source.execution_state == _EXECUTION_COMPLETED and source.vendor_bill_id is not None:
        done.append(f"Taslak fatura: Odoo kayıt #{source.vendor_bill_id}")
    if not done:
        return '<p class="text-muted">Henüz tamamlanan adım yok.</p>'
    items = "".join(f"<li>✓ {html.escape(line, quote=False)}</li>" for line in done)
    return f'<ul class="o_ipp_completed">{items}</ul>'


def _todo(instruction: str, findings: list[str] | None = None) -> str:
    body = f"<p><strong>{html.escape(instruction, quote=False)}</strong></p>"
    if findings:
        items = "".join(f"<li>⚠ {html.escape(title, quote=False)}</li>" for title in findings)
        body += f'<ul class="o_ipp_todo">{items}</ul>'
    return body


__all__ = [
    "ACCOUNT_LABEL_UNAVAILABLE",
    "MODEL_LABEL_UNAVAILABLE",
    "PURPOSE_LABELS",
    "TREATMENT_LABELS",
    "AccountingLabels",
    "REASON_TITLES",
    "WORKFLOW_LABELS",
    "GuidanceInput",
    "OperatorGuidanceFacts",
    "OperatorNextAction",
    "WorkbenchOperatorGuidance",
    "build_operator_guidance",
    "resolve_accounting_labels",
]
