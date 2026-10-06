"""Odoo Online Workbench operator request ingestion -- Hub-pull adapter (ADR-0013).

An operator in Odoo fills a small typed *request* on the Workbench projection row and
presses "İşleme Gönder"; a Studio button only snapshots the projected review version,
the requesting user and a timestamp, and sets the ready flag. The Hub tick
(:class:`OperatorRequestIngestionWorkflow`) then, per ready row:

1. parses the typed request (malformed input is rejected, never guessed);
2. derives a deterministic request key and consults the Hub request ledger;
3. authorizes the Odoo requester through the Hub-side actor directory;
4. maps the request to exactly one *existing* use case and invokes it unchanged;
5. refreshes the Workbench projection through the canonical synchronizer;
6. writes the request result back and clears the ready flag.

The adapter holds no business rule. Eligibility, write gates, named approvers, frozen
evidence and idempotency stay in the use cases; the expected version the operator saw
is passed through unchanged and never reinterpreted against a newer version.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from app.application.dto import ApplicationDTO
from app.application.exceptions.base import ApplicationError
from app.application.workbench.accounting_resolution import AccountingTreatmentType
from app.application.workbench.exceptions import (
    ReviewStateConflictError,
    ReviewVersionConflictError,
    WorkbenchCandidateReadError,
    WorkbenchContractError,
)
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workbench.supplier_resolution import SupplierResolutionMode

logger = logging.getLogger(__name__)

DEFAULT_OPERATOR_REQUEST_LIMIT = 25
#: Transient failures (Odoo unreachable, read errors) are retried on later ticks up to
#: this many attempts; then the request is closed as FAILED with a visible message.
MAX_OPERATOR_REQUEST_ATTEMPTS = 5

#: Existing API ``Permission`` claim values, kept as text so the application layer does
#: not import the API layer.
PERMISSION_REVIEW_DECIDE = "workbench_review_decide"
PERMISSION_EXECUTE = "workbench_execute"

STALE_REQUEST_MESSAGE = (
    "Bu inceleme siz işlem yaparken değişti. Güncel bilgiler yüklendi; lütfen işlemi tekrar kontrol edin."
)
UNAUTHORIZED_REQUEST_MESSAGE = "Bu işlem için Hub yetkiniz tanımlı değil. Lütfen yöneticinize başvurun."
ALREADY_COMPLETED_MESSAGE = "Bu işlem daha önce tamamlanmıştı; tekrar uygulanmadı."
FAILED_REQUEST_MESSAGE = "İşlem tamamlanamadı; teknik ekip bilgilendirilmeli."
COMPANY_MISMATCH_MESSAGE = "İstek bu şirkete ait değil; işlenmedi."


class OperatorRequestAction(StrEnum):
    """The operator actions an Odoo request may ask for -- one existing use case each."""

    SUPPLIER_RESOLUTION = "supplier_resolution"
    PURCHASE_PURPOSE = "purchase_purpose"
    ACCOUNTING_RESOLUTION = "accounting_resolution"
    DECISION = "decision"
    EXECUTE_VENDOR_BILL = "execute_vendor_bill"


#: Existing permission each action requires -- identical to its REST endpoint.
ACTION_PERMISSIONS: dict[OperatorRequestAction, frozenset[str]] = {
    OperatorRequestAction.SUPPLIER_RESOLUTION: frozenset({PERMISSION_REVIEW_DECIDE}),
    OperatorRequestAction.PURCHASE_PURPOSE: frozenset({PERMISSION_REVIEW_DECIDE}),
    OperatorRequestAction.ACCOUNTING_RESOLUTION: frozenset({PERMISSION_REVIEW_DECIDE}),
    OperatorRequestAction.DECISION: frozenset({PERMISSION_REVIEW_DECIDE}),
    OperatorRequestAction.EXECUTE_VENDOR_BILL: frozenset({PERMISSION_EXECUTE}),
}

#: Supplier modes that write an Odoo partner: they additionally need the narrow write
#: authorization (POST /write-authorizations requires workbench_execute).
WRITING_SUPPLIER_MODES = frozenset(
    {SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, SupplierResolutionMode.ONE_OFF_VENDOR}
)


class OperatorRequestOutcome(StrEnum):
    """Hub result of one request. Terminal outcomes are written back to Odoo."""

    COMPLETED = "completed"
    ALREADY_COMPLETED = "already_completed"
    STALE = "stale"
    REJECTED = "rejected"
    UNAUTHORIZED = "unauthorized"
    FAILED = "failed"
    #: Transient: nothing is written back; the request stays ready for the next tick.
    RETRY_LATER = "retry_later"


TERMINAL_OUTCOMES = frozenset(set(OperatorRequestOutcome) - {OperatorRequestOutcome.RETRY_LATER})


class OperatorRequestLedgerStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    STALE = "stale"
    REJECTED = "rejected"
    UNAUTHORIZED = "unauthorized"
    FAILED = "failed"


_LEDGER_STATUS_BY_OUTCOME: dict[OperatorRequestOutcome, OperatorRequestLedgerStatus] = {
    OperatorRequestOutcome.COMPLETED: OperatorRequestLedgerStatus.COMPLETED,
    OperatorRequestOutcome.ALREADY_COMPLETED: OperatorRequestLedgerStatus.COMPLETED,
    OperatorRequestOutcome.STALE: OperatorRequestLedgerStatus.STALE,
    OperatorRequestOutcome.REJECTED: OperatorRequestLedgerStatus.REJECTED,
    OperatorRequestOutcome.UNAUTHORIZED: OperatorRequestLedgerStatus.UNAUTHORIZED,
    OperatorRequestOutcome.FAILED: OperatorRequestLedgerStatus.FAILED,
}


@dataclass(frozen=True, slots=True)
class OperatorRequest(ApplicationDTO):
    """One typed, still-untrusted operator request read from an Odoo Workbench row."""

    odoo_record_id: int
    review_id: str
    company_id: int
    action: OperatorRequestAction
    expected_version: int
    requested_by_odoo_user_id: int
    requested_at: datetime
    supplier_mode: SupplierResolutionMode | None = None
    partner_id: int | None = None
    purchase_purpose: PurchasePurpose | None = None
    treatment_type: AccountingTreatmentType | None = None
    expense_account_id: int | None = None
    expense_category: str | None = None
    asset_account_id: int | None = None
    depreciation_model_id: int | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        _require_positive(self.odoo_record_id, "odoo_record_id must be positive.")
        if not isinstance(self.review_id, str) or not self.review_id.strip():
            raise WorkbenchContractError("review_id is required.")
        _require_positive(self.company_id, "company_id must be positive.")
        if not isinstance(self.action, OperatorRequestAction):
            raise WorkbenchContractError("action must be a canonical operator request action.")
        _require_positive(self.expected_version, "expected_version must be positive.")
        _require_positive(self.requested_by_odoo_user_id, "requested_by must be a positive Odoo user id.")
        if not isinstance(self.requested_at, datetime) or self.requested_at.utcoffset() is None:
            raise WorkbenchContractError("requested_at must be a timezone-aware datetime.")
        for name in ("partner_id", "expense_account_id", "asset_account_id", "depreciation_model_id"):
            value = getattr(self, name)
            if value is not None:
                _require_positive(value, f"{name} must be a positive ERP id when supplied.")
        self._require_action_values()

    def _require_action_values(self) -> None:
        # Shape only (which inputs an action carries). Business validity stays in the use case.
        if self.action is OperatorRequestAction.SUPPLIER_RESOLUTION and self.supplier_mode is None:
            raise WorkbenchContractError("Tedarikçi işlemi seçilmelidir.")
        if self.action is OperatorRequestAction.PURCHASE_PURPOSE and self.purchase_purpose is None:
            raise WorkbenchContractError("Satın alma amacı seçilmelidir.")
        if self.action is OperatorRequestAction.ACCOUNTING_RESOLUTION and self.treatment_type is None:
            raise WorkbenchContractError("Muhasebe işlemi seçilmelidir.")


@dataclass(frozen=True, slots=True)
class OperatorRequestReadFailure(ApplicationDTO):
    """A ready row whose request could not be parsed; reported, never guessed."""

    odoo_record_id: int
    review_id: str | None
    requested_at: datetime | None
    message: str


@dataclass(frozen=True, slots=True)
class OperatorActor(ApplicationDTO):
    """Hub identity an Odoo user acts as; permissions use existing Permission values."""

    actor: str
    permissions: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.actor, str) or not self.actor.strip():
            raise WorkbenchContractError("actor name is required.")
        object.__setattr__(self, "permissions", frozenset(self.permissions))

    def allows(self, required: frozenset[str]) -> bool:
        return required <= self.permissions


class OperatorActorDirectory:
    """Hub-side allowlist: Odoo user id -> Hub actor. Odoo identity alone grants nothing."""

    def __init__(self, actors: Mapping[int, OperatorActor]) -> None:
        self._actors = dict(actors)

    @classmethod
    def from_json(cls, raw: str | None) -> OperatorActorDirectory:
        """Parse ``{"<odoo_user_id>": {"actor": "name", "permissions": [...]}}``; empty = nobody."""

        if raw is None or not raw.strip():
            return cls({})
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WorkbenchContractError("ODOO_OPERATOR_REQUEST_ACTORS must be a JSON object.") from exc
        if not isinstance(data, dict):
            raise WorkbenchContractError("ODOO_OPERATOR_REQUEST_ACTORS must be a JSON object.")
        known = {PERMISSION_REVIEW_DECIDE, PERMISSION_EXECUTE}
        actors: dict[int, OperatorActor] = {}
        for key, value in data.items():
            if not isinstance(key, str) or not key.isdigit() or int(key) <= 0 or not isinstance(value, dict):
                raise WorkbenchContractError("Each operator actor must be keyed by a positive Odoo user id.")
            permissions = value.get("permissions")
            if not isinstance(permissions, list) or not all(isinstance(item, str) for item in permissions):
                raise WorkbenchContractError("Operator actor permissions must be a list of permission names.")
            unknown = set(permissions) - known
            if unknown:
                raise WorkbenchContractError(f"Unsupported operator actor permissions: {sorted(unknown)}.")
            actors[int(key)] = OperatorActor(actor=str(value.get("actor", "")), permissions=frozenset(permissions))
        return cls(actors)

    def resolve(self, odoo_user_id: int) -> OperatorActor | None:
        return self._actors.get(odoo_user_id)


@dataclass(frozen=True, slots=True)
class OperatorRequestLedgerEntry(ApplicationDTO):
    request_key: str
    status: OperatorRequestLedgerStatus
    attempts: int
    message: str | None = None
    authorization_id: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status is not OperatorRequestLedgerStatus.IN_PROGRESS


@dataclass(frozen=True, slots=True)
class OperatorActionOutcome(ApplicationDTO):
    """What a handler reports after invoking its existing use case."""

    outcome: OperatorRequestOutcome
    message: str


@dataclass(frozen=True, slots=True)
class OperatorRequestResult(ApplicationDTO):
    odoo_record_id: int
    review_id: str | None
    action: OperatorRequestAction | None
    outcome: OperatorRequestOutcome
    message: str
    request_key: str | None = None
    acknowledged: bool = False


@dataclass(frozen=True, slots=True)
class OperatorRequestIngestionResult(ApplicationDTO):
    company_id: int
    results: tuple[OperatorRequestResult, ...] = field(default_factory=tuple)

    def count(self, outcome: OperatorRequestOutcome) -> int:
        return sum(1 for result in self.results if result.outcome is outcome)


class OperatorRequestReader(Protocol):
    def list_pending(
        self, *, company_id: int, limit: int
    ) -> tuple[OperatorRequest | OperatorRequestReadFailure, ...]: ...


class OperatorRequestAcknowledger(Protocol):
    def acknowledge(
        self,
        *,
        odoo_record_id: int,
        requested_at: datetime | None,
        outcome: OperatorRequestOutcome,
        message: str,
        processed_at: datetime,
    ) -> bool:
        """Write the result and clear the ready flag iff the row still carries this request."""


class OperatorRequestLedger(Protocol):
    def find(self, request_key: str) -> OperatorRequestLedgerEntry | None: ...

    def start(self, *, request_key: str, request: OperatorRequest, actor: str | None) -> OperatorRequestLedgerEntry: ...

    def record_attempt(self, request_key: str, *, message: str) -> OperatorRequestLedgerEntry: ...

    def record_authorization(self, request_key: str, *, authorization_id: str) -> None: ...

    def finish(self, request_key: str, *, status: OperatorRequestLedgerStatus, message: str) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class ProjectionRefresher(Protocol):
    def sync(self, *, review_id: str, company_id: int) -> object: ...


class WriteAuthorizationIssuer(Protocol):
    def issue(
        self, *, review_id: str, company_id: int, target_version: int, operation_type: str, authorized_by: str
    ) -> str:
        """Issue one existing narrow single-use write authorization; returns its id."""


@dataclass(slots=True)
class OperatorActionContext:
    """Per-request context handed to a handler.

    ``ensure_authorization`` returns the authorization already recorded on the ledger
    for this request, or issues one through the existing use case and records it, so a
    resumed request never issues a second authorization.
    """

    actor: OperatorActor
    ensure_authorization: Callable[[str], str]
    trace_id: str | None = None


class OperatorActionHandler(Protocol):
    def handle(self, request: OperatorRequest, context: OperatorActionContext) -> OperatorActionOutcome: ...


class OperatorRequestIngestionWorkflow:
    """Consume each ready Odoo operator request through an existing Hub use case."""

    def __init__(
        self,
        *,
        reader: OperatorRequestReader,
        acknowledger: OperatorRequestAcknowledger,
        ledger: OperatorRequestLedger,
        actors: OperatorActorDirectory,
        handlers: Mapping[OperatorRequestAction, OperatorActionHandler],
        authorization_issuer: WriteAuthorizationIssuer,
        projection_refresher: ProjectionRefresher | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_attempts: int = MAX_OPERATOR_REQUEST_ATTEMPTS,
        transient_errors: tuple[type[BaseException], ...] = (),
    ) -> None:
        self._reader = reader
        self._acknowledger = acknowledger
        self._ledger = ledger
        self._actors = actors
        self._handlers = dict(handlers)
        self._authorization_issuer = authorization_issuer
        self._projection_refresher = projection_refresher
        self._clock = clock
        self._max_attempts = max_attempts
        #: Infrastructure failures (supplied by the composition root, e.g. ERP/connector
        #: errors) that mean "try again later", not "the request is wrong".
        self._transient_errors = tuple(transient_errors)

    def run(
        self, *, company_id: int, limit: int = DEFAULT_OPERATOR_REQUEST_LIMIT, trace_id: str | None = None
    ) -> OperatorRequestIngestionResult:
        _require_positive(company_id, "company_id must be positive.")
        _require_positive(limit, "limit must be positive.")
        try:
            pending = self._reader.list_pending(company_id=company_id, limit=limit)
        except WorkbenchCandidateReadError as exc:
            # Odoo unreachable: nothing was read, nothing is lost; the next tick retries.
            logger.warning("workbench.operator_request.read_failed", extra={"error": _safe(exc)})
            return OperatorRequestIngestionResult(company_id=company_id)
        results = tuple(self._process(item, company_id=company_id, trace_id=trace_id) for item in pending)
        for result in results:
            logger.info(
                "workbench.operator_request.processed",
                extra={
                    "review_id": result.review_id,
                    "odoo_record_id": result.odoo_record_id,
                    "action": result.action.value if result.action else None,
                    "outcome": result.outcome.value,
                    "acknowledged": result.acknowledged,
                },
            )
        return OperatorRequestIngestionResult(company_id=company_id, results=results)

    # ------------------------------------------------------------------ per request

    def _process(
        self, item: OperatorRequest | OperatorRequestReadFailure, *, company_id: int, trace_id: str | None
    ) -> OperatorRequestResult:
        if isinstance(item, OperatorRequestReadFailure):
            message = f"İstek okunamadı: {item.message}"
            acknowledged = self._acknowledge(
                item.odoo_record_id, item.requested_at, OperatorRequestOutcome.REJECTED, message
            )
            return OperatorRequestResult(
                odoo_record_id=item.odoo_record_id,
                review_id=item.review_id,
                action=None,
                outcome=OperatorRequestOutcome.REJECTED,
                message=message,
                acknowledged=acknowledged,
            )
        request = item
        key = operator_request_key(request)
        if request.company_id != company_id:
            return self._close_without_ledger(request, key, OperatorRequestOutcome.REJECTED, COMPANY_MISMATCH_MESSAGE)

        entry = self._ledger.find(key)
        if entry is not None and entry.terminal:
            # Crash after Hub commit but before Odoo acknowledgement: re-acknowledge only.
            outcome = _outcome_for_terminal(entry.status)
            return self._finish(request, key, outcome, entry.message or "", refresh=True)

        actor = self._actors.resolve(request.requested_by_odoo_user_id)
        if actor is None or not actor.allows(ACTION_PERMISSIONS[request.action]):
            return self._close(request, key, entry, OperatorRequestOutcome.UNAUTHORIZED, UNAUTHORIZED_REQUEST_MESSAGE)

        if entry is None:
            entry = self._ledger.start(request_key=key, request=request, actor=actor.actor)
            self._ledger.commit()

        handler = self._handlers.get(request.action)
        if handler is None:
            return self._close(request, key, entry, OperatorRequestOutcome.REJECTED, "Bu işlem henüz desteklenmiyor.")

        context = OperatorActionContext(
            actor=actor,
            ensure_authorization=self._authorization_provider(request, key, entry, actor),
            trace_id=trace_id,
        )
        try:
            outcome = handler.handle(request, context)
        except (ReviewVersionConflictError, ReviewStateConflictError):
            self._ledger.rollback()
            outcome = OperatorActionOutcome(outcome=OperatorRequestOutcome.STALE, message=STALE_REQUEST_MESSAGE)
        except WorkbenchCandidateReadError as exc:
            self._ledger.rollback()
            outcome = OperatorActionOutcome(outcome=OperatorRequestOutcome.RETRY_LATER, message=_safe(exc))
        except self._transient_errors as exc:
            self._ledger.rollback()
            outcome = OperatorActionOutcome(outcome=OperatorRequestOutcome.RETRY_LATER, message=_safe(exc))
        except ApplicationError as exc:
            self._ledger.rollback()
            outcome = _classified_application_error(exc)
        except Exception as exc:  # noqa: BLE001 - logged with traceback and surfaced; never silently dropped
            self._ledger.rollback()
            logger.exception(
                "workbench.operator_request.unexpected_error",
                extra={"review_id": request.review_id, "action": request.action.value},
            )
            outcome = OperatorActionOutcome(
                outcome=OperatorRequestOutcome.FAILED, message=f"{FAILED_REQUEST_MESSAGE} ({type(exc).__name__})"
            )

        if outcome.outcome is OperatorRequestOutcome.RETRY_LATER:
            return self._retry_later(request, key, outcome.message)
        return self._close(request, key, entry, outcome.outcome, outcome.message)

    def _authorization_provider(
        self, request: OperatorRequest, key: str, entry: OperatorRequestLedgerEntry, actor: OperatorActor
    ) -> Callable[[str], str]:
        recorded = entry.authorization_id

        def ensure(operation_type: str) -> str:
            nonlocal recorded
            if recorded is not None:
                return recorded
            if PERMISSION_EXECUTE not in actor.permissions:
                raise _AuthorizationNotPermittedError()
            authorization_id = self._authorization_issuer.issue(
                review_id=request.review_id,
                company_id=request.company_id,
                target_version=request.expected_version,
                operation_type=operation_type,
                authorized_by=actor.actor,
            )
            self._ledger.record_authorization(key, authorization_id=authorization_id)
            self._ledger.commit()
            recorded = authorization_id
            return authorization_id

        return ensure

    def _retry_later(self, request: OperatorRequest, key: str, message: str) -> OperatorRequestResult:
        entry = self._ledger.record_attempt(key, message=message)
        self._ledger.commit()
        if entry.attempts >= self._max_attempts:
            return self._close(
                request, key, entry, OperatorRequestOutcome.FAILED, f"{FAILED_REQUEST_MESSAGE} ({message})"
            )
        return OperatorRequestResult(
            odoo_record_id=request.odoo_record_id,
            review_id=request.review_id,
            action=request.action,
            outcome=OperatorRequestOutcome.RETRY_LATER,
            message=message,
            request_key=key,
        )

    def _close(
        self,
        request: OperatorRequest,
        key: str,
        entry: OperatorRequestLedgerEntry | None,
        outcome: OperatorRequestOutcome,
        message: str,
    ) -> OperatorRequestResult:
        if entry is None:
            self._ledger.start(request_key=key, request=request, actor=None)
        self._ledger.finish(key, status=_LEDGER_STATUS_BY_OUTCOME[outcome], message=message)
        self._ledger.commit()
        return self._finish(request, key, outcome, message, refresh=outcome is not OperatorRequestOutcome.UNAUTHORIZED)

    def _close_without_ledger(
        self, request: OperatorRequest, key: str, outcome: OperatorRequestOutcome, message: str
    ) -> OperatorRequestResult:
        acknowledged = self._acknowledge(request.odoo_record_id, request.requested_at, outcome, message)
        return OperatorRequestResult(
            odoo_record_id=request.odoo_record_id,
            review_id=request.review_id,
            action=request.action,
            outcome=outcome,
            message=message,
            request_key=key,
            acknowledged=acknowledged,
        )

    def _finish(
        self, request: OperatorRequest, key: str, outcome: OperatorRequestOutcome, message: str, *, refresh: bool
    ) -> OperatorRequestResult:
        if refresh and self._projection_refresher is not None:
            # The synchronizer never raises; it logs and reports its own failures.
            self._projection_refresher.sync(review_id=request.review_id, company_id=request.company_id)
        acknowledged = self._acknowledge(request.odoo_record_id, request.requested_at, outcome, message)
        return OperatorRequestResult(
            odoo_record_id=request.odoo_record_id,
            review_id=request.review_id,
            action=request.action,
            outcome=outcome,
            message=message,
            request_key=key,
            acknowledged=acknowledged,
        )

    def _acknowledge(
        self, odoo_record_id: int, requested_at: datetime | None, outcome: OperatorRequestOutcome, message: str
    ) -> bool:
        try:
            return self._acknowledger.acknowledge(
                odoo_record_id=odoo_record_id,
                requested_at=requested_at,
                outcome=outcome,
                message=message,
                processed_at=self._clock(),
            )
        except ApplicationError as exc:
            # Hub state is authoritative and the ledger is terminal: the next tick re-acknowledges.
            logger.warning(
                "workbench.operator_request.acknowledge_failed",
                extra={"odoo_record_id": odoo_record_id, "error": _safe(exc)},
            )
            return False


class _AuthorizationNotPermittedError(ApplicationError):
    safe_message = "Bu yazma işlemi için yetki (workbench_execute) gerekiyor."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


def operator_request_key(request: OperatorRequest) -> str:
    """Deterministic request identity; the same submitted request always maps here."""

    payload = {
        "company_id": request.company_id,
        "review_id": request.review_id,
        "odoo_record_id": request.odoo_record_id,
        "action": request.action.value,
        "expected_version": request.expected_version,
        "requested_by": request.requested_by_odoo_user_id,
        "requested_at": request.requested_at.astimezone(UTC).isoformat(),
        "supplier_mode": request.supplier_mode.value if request.supplier_mode else None,
        "partner_id": request.partner_id,
        "purchase_purpose": request.purchase_purpose.value if request.purchase_purpose else None,
        "treatment_type": request.treatment_type.value if request.treatment_type else None,
        "expense_account_id": request.expense_account_id,
        "expense_category": request.expense_category,
        "asset_account_id": request.asset_account_id,
        "depreciation_model_id": request.depreciation_model_id,
        "note": request.note,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"odoo-operator-request:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _outcome_for_terminal(status: OperatorRequestLedgerStatus) -> OperatorRequestOutcome:
    return {
        OperatorRequestLedgerStatus.COMPLETED: OperatorRequestOutcome.COMPLETED,
        OperatorRequestLedgerStatus.STALE: OperatorRequestOutcome.STALE,
        OperatorRequestLedgerStatus.REJECTED: OperatorRequestOutcome.REJECTED,
        OperatorRequestLedgerStatus.UNAUTHORIZED: OperatorRequestOutcome.UNAUTHORIZED,
        OperatorRequestLedgerStatus.FAILED: OperatorRequestOutcome.FAILED,
    }[status]


def _classified_application_error(exc: ApplicationError) -> OperatorActionOutcome:
    """A use case refused the request: show its safe reason. Never retried automatically."""

    return OperatorActionOutcome(outcome=OperatorRequestOutcome.REJECTED, message=f"İşlem reddedildi: {_safe(exc)}")


def _safe(exc: BaseException) -> str:
    message = getattr(exc, "safe_message", None)
    if isinstance(message, str) and message.strip():
        return message.strip()
    text = str(exc).strip()
    return text or type(exc).__name__


def _require_positive(value: object, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)


__all__ = [
    "ACTION_PERMISSIONS",
    "ALREADY_COMPLETED_MESSAGE",
    "DEFAULT_OPERATOR_REQUEST_LIMIT",
    "MAX_OPERATOR_REQUEST_ATTEMPTS",
    "PERMISSION_EXECUTE",
    "PERMISSION_REVIEW_DECIDE",
    "STALE_REQUEST_MESSAGE",
    "TERMINAL_OUTCOMES",
    "UNAUTHORIZED_REQUEST_MESSAGE",
    "WRITING_SUPPLIER_MODES",
    "OperatorActionContext",
    "OperatorActionHandler",
    "OperatorActionOutcome",
    "OperatorActor",
    "OperatorActorDirectory",
    "OperatorRequest",
    "OperatorRequestAcknowledger",
    "OperatorRequestAction",
    "OperatorRequestIngestionResult",
    "OperatorRequestIngestionWorkflow",
    "OperatorRequestLedger",
    "OperatorRequestLedgerEntry",
    "OperatorRequestLedgerStatus",
    "OperatorRequestOutcome",
    "OperatorRequestReadFailure",
    "OperatorRequestReader",
    "OperatorRequestResult",
    "ProjectionRefresher",
    "WriteAuthorizationIssuer",
    "operator_request_key",
]
