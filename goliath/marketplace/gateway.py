"""Marketplace tool gateway.

Every marketplace read/write flows through here. The gateway enforces agent
identity/scope, account and marketplace scope, operation capability, automation
mode, emergency stop, circuit breakers, account health, and idempotency; executes
the typed adapter operation via the session broker (never touching cookies);
verifies writes with a read-back; persists an operation attempt and audit event;
classifies errors; and returns a typed receipt.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from goliath.config import OrchestrationConfig
from goliath.db.marketplace_repositories import (
    CircuitBreakerRepository,
    EmergencyStopRepository,
    ExceptionTaskRepository,
    MarketplaceAccountRepository,
    OperationAttemptRepository,
)
from goliath.db.models import (
    AUTONOMOUS_WRITE_MODES,
    WRITE_FORBIDDEN_ACCOUNT_STATUSES,
    AutomationMode,
    MarketplaceAccount,
    OperationStatus,
    utc_now,
)
from goliath.db.repositories import AuditEventRepository, RecordNotFoundError
from goliath.marketplace.adapter import MarketplaceAdapter, OperationResult
from goliath.marketplace.broker import SessionBroker

# Operations that write to a marketplace (require autonomous mode + breaker checks).
WRITE_OPERATIONS = frozenset(
    {
        "create_listing",
        "update_listing",
        "refresh_listing",
        "promote_listing",
        "share_listing",
        "end_listing",
        "accept_offer",
        "decline_offer",
        "counter_offer",
        "send_offer",
        "send_message",
        "update_tracking",
        "purchase_label",
    }
)


class GatewayError(RuntimeError):
    def __init__(self, message: str, *, category: str = "invalid") -> None:
        super().__init__(message)
        self.category = category


@dataclass(slots=True)
class MarketplaceMetrics:
    reads: int = 0
    writes: int = 0
    write_failures: int = 0
    verification_failures: int = 0
    auth_failures: int = 0
    rate_limit_events: int = 0
    breaker_openings: int = 0
    shadow_writes: int = 0
    by_operation: Counter[str] = field(default_factory=Counter)


@dataclass(slots=True)
class OperationReceipt:
    ok: bool
    operation: str
    executed: bool
    shadowed: bool = False
    verified: bool = False
    remote_id: str | None = None
    remote_url: str | None = None
    error_category: str | None = None
    attempt_id: str | None = None
    mode: str | None = None
    reasons: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    retry_after_seconds: int | None = None


class MarketplaceGateway:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | sessionmaker[Session],
        config: OrchestrationConfig,
        broker: SessionBroker,
        metrics: MarketplaceMetrics | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._marketplace = config.marketplace
        self._broker = broker
        self.metrics = metrics or MarketplaceMetrics()

    # ------------------------------------------------------------------ public
    async def execute(
        self,
        *,
        account_id: UUID,
        operation: str,
        scope: str,
        principal_scopes: set[str] | None,
        adapter_call: Callable[[MarketplaceAdapter], Awaitable[OperationResult]],
        idempotency_key: str | None = None,
        verify_call: Callable[[MarketplaceAdapter], Awaitable[bool]] | None = None,
        agent_identity: str | None = None,
        resource_type: str | None = None,
        resource_id: UUID | None = None,
        request_summary: dict[str, Any] | None = None,
    ) -> OperationReceipt:
        is_write = operation in WRITE_OPERATIONS
        if is_write and not idempotency_key:
            return OperationReceipt(
                ok=False,
                operation=operation,
                executed=False,
                error_category="invalid",
                reasons=["every marketplace write requires an idempotency key"],
            )
        if not self._scope_ok(scope, principal_scopes, account_id):
            self.metrics.by_operation[operation] += 1
            return OperationReceipt(
                ok=False,
                operation=operation,
                executed=False,
                error_category="unauthorized",
                reasons=[f"scope required: {scope}"],
            )

        account, mode = self._load_account(account_id)
        mode = self._effective_mode(account, operation, mode)
        self.metrics.by_operation[operation] += 1

        block = self._precheck(account, mode, operation, is_write=is_write, resource_id=resource_id)
        if block is not None:
            return block

        # Idempotent replay for writes.
        if is_write and idempotency_key:
            replay = self._idempotent_replay(idempotency_key, operation, mode)
            if replay is not None:
                return replay

        # Shadow mode: compute the intent, record it, but do not execute the write.
        if is_write and mode in {AutomationMode.SHADOW, AutomationMode.SIMULATION}:
            self.metrics.shadow_writes += 1
            attempt_id = self._record_attempt(
                account_id,
                operation,
                idempotency_key or f"shadow-{utc_now().timestamp()}",
                mode,
                agent_identity,
                resource_type,
                resource_id,
                request_summary,
                status=OperationStatus.SHADOWED,
                result={"shadowed": True},
            )
            self._audit(account_id, f"marketplace.{operation}.shadowed", {"mode": mode.value})
            return OperationReceipt(
                ok=True,
                operation=operation,
                executed=False,
                shadowed=True,
                mode=mode.value,
                attempt_id=attempt_id,
                reasons=["shadow mode: write not executed"],
            )

        adapter = self._broker.acquire_adapter(account_id)
        if operation not in adapter.capabilities() and operation not in {
            "health_check",
            "authentication_status",
        }:
            return self._fail(
                account_id,
                operation,
                mode,
                "not_supported",
                is_write,
                reasons=[f"adapter does not support {operation}"],
            )

        result = await adapter_call(adapter)
        if is_write:
            self.metrics.writes += 1
        else:
            self.metrics.reads += 1

        if not result.ok:
            return self._handle_failure(
                account,
                operation,
                mode,
                result,
                is_write,
                agent_identity,
                idempotency_key,
                resource_type,
                resource_id,
                request_summary,
            )

        verified = False
        if is_write and verify_call is not None:
            try:
                verified = await verify_call(adapter)
            except Exception:  # noqa: BLE001 - verification must never crash the flow
                verified = False
            if not verified:
                self.metrics.verification_failures += 1
                self._open_exception(
                    account_id,
                    "verification_failed",
                    operation,
                    "post-write verification was inconclusive",
                    resource_id=resource_id,
                )
                self._audit(
                    account_id,
                    "marketplace.verification_failed",
                    {"operation": operation},
                )
        elif is_write:
            verified = self._result_verifies_write(operation, result)
            if not verified:
                self.metrics.verification_failures += 1
                self._open_exception(
                    account_id,
                    "verification_failed",
                    operation,
                    "adapter did not provide conclusive post-write verification",
                    resource_id=resource_id,
                )
                self._audit(
                    account_id,
                    "marketplace.verification_failed",
                    {"operation": operation},
                )

        attempt_id = self._record_attempt(
            account_id,
            operation,
            idempotency_key or f"op-{utc_now().timestamp()}",
            mode,
            agent_identity,
            resource_type,
            resource_id,
            request_summary,
            status=OperationStatus.VERIFIED if verified else OperationStatus.SUCCEEDED,
            result=result.data,
            remote_identifier=result.remote_id,
            remote_request_id=result.remote_request_id,
            verified=verified,
        )
        if is_write:
            self._record_breaker_success(account_id, operation)
        self._audit(
            account_id,
            f"marketplace.{operation}",
            {"remote_id": result.remote_id, "verified": verified, "mode": mode.value},
        )
        if is_write and mode in AUTONOMOUS_WRITE_MODES:
            self._audit(
                account_id,
                "marketplace.autonomous_action",
                {"operation": operation, "verified": verified, "mode": mode.value},
            )
        return OperationReceipt(
            ok=not is_write or verified,
            operation=operation,
            executed=True,
            verified=verified,
            remote_id=result.remote_id,
            remote_url=result.remote_url,
            mode=mode.value,
            attempt_id=attempt_id,
            data=result.data,
            error_category="verification_failed" if is_write and not verified else None,
            reasons=(
                ["post-write verification was inconclusive"] if is_write and not verified else []
            ),
        )

    # ---------------------------------------------------------------- internals
    def _scope_ok(self, scope: str, principal_scopes: set[str] | None, account_id: UUID) -> bool:
        if principal_scopes is None:
            return True  # trusted internal caller (scheduler/service)
        return (
            scope in principal_scopes
            or f"{scope}@{account_id}" in principal_scopes
            or "admin" in principal_scopes
        )

    @staticmethod
    def _result_verifies_write(operation: str, result: OperationResult) -> bool:
        """Accept only explicit adapter confirmations when a read-back is unavailable."""
        data = result.data
        checks = {
            "update_listing": bool(result.remote_id and data.get("status")),
            "refresh_listing": data.get("refreshed") is True,
            "promote_listing": data.get("promoted") is True,
            "share_listing": data.get("shared") is True,
            "accept_offer": data.get("state") == "accepted",
            "decline_offer": data.get("state") == "declined",
            "counter_offer": data.get("state") == "countered",
            "send_offer": data.get("sent") is True,
            "send_message": data.get("delivered") is True,
            "update_tracking": bool(data.get("tracking_number")),
            "purchase_label": bool(data.get("label_reference") and data.get("tracking_number")),
        }
        return bool(checks.get(operation, False))

    def _load_account(self, account_id: UUID) -> tuple[MarketplaceAccount, AutomationMode]:
        with self._session_factory() as session:
            account = MarketplaceAccountRepository(session).get(account_id)
            if account is None:
                raise RecordNotFoundError(f"marketplace account not found: {account_id}")
            session.expunge(account)
            return account, account.automation_mode

    def _effective_mode(
        self, account: MarketplaceAccount, operation: str, persisted: AutomationMode
    ) -> AutomationMode:
        """Resolve operation override, then account override, then persisted account mode."""
        configured = next(
            (
                value
                for value in self._marketplace.accounts.values()
                if value.marketplace == account.marketplace.value
                and value.label == account.account_label
            ),
            None,
        )
        mode = persisted
        if configured and operation in configured.operation_modes:
            mode = AutomationMode(configured.operation_modes[operation])
        if operation in self._marketplace.operation_modes:
            mode = AutomationMode(self._marketplace.operation_modes[operation])
        return mode

    def _precheck(
        self,
        account: MarketplaceAccount,
        mode: AutomationMode,
        operation: str,
        *,
        is_write: bool,
        resource_id: UUID | None = None,
    ) -> OperationReceipt | None:
        if mode is AutomationMode.DISABLED:
            return self._blocked(operation, mode, ["automation disabled for account"])
        if not is_write:
            return None  # reads are permitted in every non-disabled mode
        with self._session_factory() as session:
            stops = EmergencyStopRepository(session)
            stop_keys = {
                str(account.id),
                f"marketplace:{account.marketplace.value}",
                f"operation:{account.id}:{operation}",
            }
            if resource_id is not None:
                stop_keys.add(f"resource:{resource_id}")
            if any(stops.is_active(key) for key in stop_keys):
                return self._blocked(operation, mode, ["emergency stop active"])
            breaker = CircuitBreakerRepository(session)
            if breaker.is_open(scope="global", scope_key="*"):
                session.commit()
                return self._blocked(operation, mode, ["global circuit breaker open"])
            if breaker.is_open(scope="adapter", scope_key=account.marketplace.value):
                session.commit()
                return self._blocked(operation, mode, ["marketplace adapter circuit breaker open"])
            if breaker.is_open(scope="account", scope_key=str(account.id)):
                session.commit()
                return self._blocked(operation, mode, ["account circuit breaker open"])
            if breaker.is_open(scope="operation", scope_key=f"{account.id}:{operation}"):
                session.commit()
                return self._blocked(operation, mode, ["operation circuit breaker open"])
            attempts = OperationAttemptRepository(session)
            if (
                attempts.count_since(since=utc_now() - timedelta(minutes=1), account_id=account.id)
                >= self._marketplace.operation_rate_limit_per_minute
            ):
                return self._blocked(operation, mode, ["configured operation rate limit reached"])
            if (
                operation == "create_listing"
                and attempts.count_since(
                    since=utc_now() - timedelta(hours=1), operation="create_listing"
                )
                >= self._marketplace.max_publish_per_hour
            ):
                return self._blocked(operation, mode, ["configured publish volume reached"])
            if mode is AutomationMode.CANARY:
                canary = self._marketplace.canary
                if canary.allowed_account_ids and str(account.id) not in canary.allowed_account_ids:
                    return self._blocked(operation, mode, ["account is outside canary allowlist"])
                if canary.allowed_operations and operation not in canary.allowed_operations:
                    return self._blocked(operation, mode, ["operation is outside canary allowlist"])
                if (
                    resource_id is not None
                    and canary.allowed_inventory_ids
                    and str(resource_id) not in canary.allowed_inventory_ids
                ):
                    return self._blocked(operation, mode, ["resource is outside canary allowlist"])
                if (
                    attempts.count_since(
                        since=utc_now() - timedelta(hours=1), account_id=account.id
                    )
                    >= canary.maximum_writes_per_hour
                ):
                    return self._blocked(operation, mode, ["canary hourly write limit reached"])
            session.commit()
        if account.status in WRITE_FORBIDDEN_ACCOUNT_STATUSES:
            return self._blocked(
                operation, mode, [f"account status forbids writes: {account.status.value}"]
            )
        if mode is AutomationMode.PAUSED:
            return self._blocked(operation, mode, ["account is paused"])
        if mode is AutomationMode.OBSERVE:
            return self._blocked(operation, mode, ["observe mode: reads only"])
        if mode not in AUTONOMOUS_WRITE_MODES and mode not in {
            AutomationMode.SHADOW,
            AutomationMode.SIMULATION,
        }:
            return self._blocked(operation, mode, [f"writes not permitted in mode {mode.value}"])
        return None

    def _idempotent_replay(
        self, idempotency_key: str, operation: str, mode: AutomationMode
    ) -> OperationReceipt | None:
        with self._session_factory() as session:
            existing = OperationAttemptRepository(session).get_by_idempotency(idempotency_key)
            if existing is not None and existing.status in {
                OperationStatus.SUCCEEDED,
                OperationStatus.VERIFIED,
            }:
                return OperationReceipt(
                    ok=True,
                    operation=operation,
                    executed=False,
                    verified=existing.verified,
                    remote_id=existing.remote_identifier,
                    remote_url=(existing.result_summary or {}).get("remote_url"),
                    mode=mode.value,
                    attempt_id=str(existing.id),
                    reasons=["idempotent replay"],
                    data=dict(existing.result_summary or {}),
                )
        return None

    def _handle_failure(
        self,
        account,
        operation,
        mode,
        result,
        is_write,
        agent_identity,
        idempotency_key,
        resource_type,
        resource_id,
        request_summary,
    ) -> OperationReceipt:
        category = result.error_category or "temporary_remote"
        category = {
            "auth_required": "authentication",
            "rate_limited": "rate_limit",
            "not_supported": "unsupported",
            "verification_failed": "verification",
            "transient": "temporary_remote",
            "invalid": "validation",
        }.get(category, category)
        if is_write:
            self.metrics.write_failures += 1
        if category == "authentication":
            self.metrics.auth_failures += 1
            self._require_reauth(account.id)
        if category == "rate_limit":
            self.metrics.rate_limit_events += 1
        self._record_attempt(
            account.id,
            operation,
            idempotency_key or f"fail-{utc_now().timestamp()}",
            mode,
            agent_identity,
            resource_type,
            resource_id,
            request_summary,
            status=OperationStatus.FAILED,
            result=result.data,
            error_category=category,
            remote_request_id=result.remote_request_id,
        )
        if is_write and category not in {
            "rate_limit",
            "validation",
            "unsupported",
            "not_found",
            "conflict",
        }:
            self._record_breaker_failure(account.id, operation, category)
        self._audit(account.id, f"marketplace.{operation}.failed", {"category": category})
        return OperationReceipt(
            ok=False,
            operation=operation,
            executed=is_write,
            error_category=category,
            mode=mode.value,
            reasons=[result.data.get("detail", category)],
            retry_after_seconds=result.retry_after_seconds,
        )

    def _blocked(self, operation, mode, reasons) -> OperationReceipt:
        return OperationReceipt(
            ok=False,
            operation=operation,
            executed=False,
            error_category="blocked",
            mode=mode.value,
            reasons=reasons,
        )

    def _fail(self, account_id, operation, mode, category, is_write, reasons) -> OperationReceipt:
        if is_write:
            self.metrics.write_failures += 1
        self._audit(account_id, f"marketplace.{operation}.failed", {"category": category})
        return OperationReceipt(
            ok=False,
            operation=operation,
            executed=False,
            error_category=category,
            mode=mode.value,
            reasons=reasons,
        )

    def _record_attempt(
        self,
        account_id,
        operation,
        idempotency_key,
        mode,
        agent_identity,
        resource_type,
        resource_id,
        request_summary,
        *,
        status,
        result=None,
        remote_identifier=None,
        remote_request_id=None,
        verified=False,
        error_category=None,
    ) -> str:
        # Successful attempts keep the real idempotency key so replays can find
        # them; non-success attempts get a unique key so retries never collide.
        stored_key = idempotency_key
        if status not in {OperationStatus.SUCCEEDED, OperationStatus.VERIFIED}:
            stored_key = f"{idempotency_key}:{secrets.token_hex(6)}"
        with self._session_factory() as session:
            repo = OperationAttemptRepository(session)
            attempt = repo.start(
                account_id=account_id,
                operation=operation,
                idempotency_key=stored_key,
                automation_mode=mode.value,
                agent_identity=agent_identity,
                resource_type=resource_type,
                resource_id=resource_id,
                request_summary=request_summary or {},
                requested_payload_hash=hashlib.sha256(
                    json.dumps(
                        request_summary or {},
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode()
                ).hexdigest(),
            )
            repo.complete(
                attempt.id,
                status=status,
                result_summary=result or {},
                remote_identifier=remote_identifier,
                verified=verified,
                error_category=error_category,
                remote_request_id=remote_request_id,
            )
            session.commit()
            return str(attempt.id)

    def _record_breaker_failure(self, account_id, operation, category) -> None:
        rules = self._marketplace.circuit_breaker
        with self._session_factory() as session:
            repo = CircuitBreakerRepository(session)
            account_breaker = repo.get_or_create(
                scope="account", scope_key=str(account_id), threshold=rules.failure_threshold
            )
            account_was_open = account_breaker.state.value == "open"
            account_after = repo.record_failure(
                scope="account",
                scope_key=str(account_id),
                threshold=rules.failure_threshold,
                cooldown_seconds=rules.cooldown_seconds,
                reason=f"{operation}:{category}",
            )
            operation_breaker = repo.get_or_create(
                scope="operation",
                scope_key=f"{account_id}:{operation}",
                threshold=rules.failure_threshold,
            )
            operation_was_open = operation_breaker.state.value == "open"
            operation_after = repo.record_failure(
                scope="operation",
                scope_key=f"{account_id}:{operation}",
                threshold=rules.failure_threshold,
                cooldown_seconds=rules.cooldown_seconds,
                reason=category,
            )
            opened = None
            if not account_was_open and account_after.state.value == "open":
                opened = account_after
            elif not operation_was_open and operation_after.state.value == "open":
                opened = operation_after
            if opened is not None:
                self.metrics.breaker_openings += 1
                AuditEventRepository(session).append(
                    event_type="breaker.opened",
                    actor_type="system",
                    actor_id="marketplace-gateway",
                    resource_type="circuit_breaker",
                    resource_id=opened.id,
                    details={"scope": opened.scope, "operation": operation},
                )
            session.commit()

    def _record_breaker_success(self, account_id, operation) -> None:
        with self._session_factory() as session:
            repo = CircuitBreakerRepository(session)
            repo.record_success(scope="account", scope_key=str(account_id))
            repo.record_success(scope="operation", scope_key=f"{account_id}:{operation}")
            session.commit()

    def _require_reauth(self, account_id) -> None:
        from goliath.db.models import MarketplaceAccountStatus

        with self._session_factory() as session:
            MarketplaceAccountRepository(session).set_status(
                account_id, status=MarketplaceAccountStatus.AUTHENTICATION_REQUIRED
            )
            session.commit()

    def _open_exception(
        self, account_id, exception_type, operation, reason, *, resource_id=None
    ) -> None:
        with self._session_factory() as session:
            ExceptionTaskRepository(session).create(
                exception_type=exception_type,
                reason=reason,
                severity="high",
                resource_type="marketplace_operation",
                resource_id=resource_id,
                account_id=account_id,
                detail={"operation": operation},
            )
            session.commit()

    def _audit(self, account_id, event_type, details) -> None:
        # Never include cookies, credentials, or full remote payloads.
        with self._session_factory() as session:
            AuditEventRepository(session).append(
                event_type=event_type,
                actor_type="system",
                actor_id="marketplace-gateway",
                resource_type="marketplace_account",
                resource_id=account_id,
                details=details,
            )
            session.commit()
