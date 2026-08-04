"""Session broker: the sole boundary where session state is decrypted.

Responsibilities: store/retrieve the encrypted session reference, decrypt only
inside the adapter boundary, provision an isolated per-account session directory
with restrictive permissions outside any agent workspace, enforce the domain
allowlist and marketplace/account identity, and return only a typed adapter.

It never returns cookies or browser storage to a caller, never logs plaintext,
and pauses writes when reauthentication is required.
"""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from goliath.config import OrchestrationConfig
from goliath.db.marketplace_repositories import (
    MarketplaceAccountRepository,
    SessionReferenceRepository,
)
from goliath.db.models import MarketplaceAccountStatus, utc_now
from goliath.db.repositories import AuditEventRepository, RecordNotFoundError
from goliath.marketplace.adapter import MarketplaceAdapter
from goliath.marketplace.cipher import SessionCipher
from goliath.marketplace.fake_adapter import FakeMarketplaceAdapter
from goliath.marketplace.manual_adapter import ManualMarketplaceAdapter


class SessionBrokerError(RuntimeError):
    pass


# A factory builds an adapter from decrypted session bytes + isolation context.
AdapterFactory = Callable[["AdapterProvisioning"], MarketplaceAdapter]


class AdapterProvisioning:
    """Everything an adapter needs — never exposed to agents."""

    def __init__(
        self,
        *,
        marketplace: str,
        account_id: UUID,
        allowed_domains: list[str],
        storage_dir: Path,
        session_state: bytes,
        browser_executable: str | None,
    ) -> None:
        self.marketplace = marketplace
        self.account_id = account_id
        self.allowed_domains = allowed_domains
        self.storage_dir = storage_dir
        self.session_state = session_state  # decrypted; adapter-private
        self.browser_executable = browser_executable


class SessionBroker:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | sessionmaker[Session],
        config: OrchestrationConfig,
        cipher: SessionCipher | None = None,
        adapter_factory: AdapterFactory | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._session_config = config.marketplace.session
        self._cipher = cipher or self._build_cipher()
        self._custom_adapter_factory = adapter_factory is not None
        self._adapter_factory = adapter_factory or self._production_adapter

    @staticmethod
    def _production_adapter(provisioning: AdapterProvisioning) -> MarketplaceAdapter:
        """Select an explicit adapter; never silently simulate a production write."""
        if provisioning.marketplace == "fake":
            return FakeMarketplaceAdapter(marketplace="fake")
        if provisioning.marketplace == "generic":
            return ManualMarketplaceAdapter()
        from goliath.marketplace.browser_adapter import BROWSER_ADAPTERS

        adapter_type = BROWSER_ADAPTERS.get(provisioning.marketplace)
        if adapter_type is None:
            raise SessionBrokerError(
                f"no adapter registered for marketplace: {provisioning.marketplace}"
            )
        if not provisioning.allowed_domains:
            raise SessionBrokerError("browser marketplace account has no domain allowlist")
        return adapter_type(
            storage_state=provisioning.session_state,
            allowed_domains=provisioning.allowed_domains,
            browser_executable=provisioning.browser_executable,
        )

    def _build_cipher(self) -> SessionCipher:
        key = self._session_config.encryption_key or os.environ.get("GOLIATH_SESSION_KEY")
        if not key:
            # Ephemeral key: encryption still applies at rest for this process.
            key = SessionCipher.generate_key()
        return SessionCipher(key)

    def _session_root(self) -> Path:
        root = self._session_config.session_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def store_session(
        self,
        account_id: UUID,
        session_state: bytes,
        *,
        allowed_domains: list[str],
        actor: str = "human:operator",
    ) -> str:
        """Encrypt and persist session state; provision an isolated 0700 directory."""
        if not session_state:
            raise SessionBrokerError("empty session state")
        reference_key = f"sess_{secrets.token_urlsafe(24)}"
        storage_dir = self._session_root() / reference_key
        storage_dir.mkdir(parents=True, exist_ok=True)
        # Restrictive permissions: owner-only.
        os.chmod(storage_dir, stat.S_IRWXU)
        ciphertext = self._cipher.encrypt(session_state)
        expires_at = utc_now() + timedelta(seconds=self._session_config.session_ttl_seconds)
        with self._session_factory() as session:
            references = SessionReferenceRepository(session)
            previous = references.get_active(account_id)
            if previous is not None:
                references.revoke(previous.reference_key)
            references.store(
                account_id=account_id,
                reference_key=reference_key,
                ciphertext=ciphertext,
                key_id=self._cipher.key_id,
                storage_dir=str(storage_dir),
                allowed_domains=allowed_domains,
                expires_at=expires_at,
            )
            accounts = MarketplaceAccountRepository(session)
            account = accounts.require(account_id)
            accounts.set_session(account_id, reference=reference_key, expires_at=expires_at)
            account.last_authenticated_at = utc_now()
            accounts.set_status(account_id, status=MarketplaceAccountStatus.HEALTHY)
            AuditEventRepository(session).append(
                event_type="session.stored",
                actor_type="human",
                actor_id=actor.partition(":")[2] or actor,
                resource_type="marketplace_account",
                resource_id=account_id,
                details={"reference_present": True},  # never the ciphertext/plaintext
            )
            if previous is not None:
                AuditEventRepository(session).append(
                    event_type="session.rotated",
                    actor_type="human",
                    actor_id=actor.partition(":")[2] or actor,
                    resource_type="marketplace_account",
                    resource_id=account_id,
                    details={},
                )
            session.commit()
        if previous is not None:
            previous_dir = Path(previous.storage_dir).resolve()
            root = self._session_root()
            if previous_dir.is_relative_to(root) and previous_dir != root:
                try:
                    previous_dir.rmdir()
                except OSError:
                    # A non-empty directory is retained for operator inspection; never recurse.
                    pass
        return reference_key

    def acquire_adapter(self, account_id: UUID) -> MarketplaceAdapter:
        """Return a typed adapter for the account. Never returns session bytes."""
        with self._session_factory() as session:
            accounts = MarketplaceAccountRepository(session)
            account = accounts.get(account_id)
            if account is None:
                raise RecordNotFoundError(f"marketplace account not found: {account_id}")
            if account.status is MarketplaceAccountStatus.AUTHENTICATION_REQUIRED:
                raise SessionBrokerError("account requires reauthentication; writes are paused")
            reference = SessionReferenceRepository(session).get_active(account_id)
            marketplace = account.marketplace.value
            session.expunge(account)

        if reference is None:
            # No stored session: fake/manual adapters may still operate for tests.
            provisioning = AdapterProvisioning(
                marketplace=marketplace,
                account_id=account_id,
                allowed_domains=[],
                storage_dir=self._session_root() / "unbound",
                session_state=b"",
                browser_executable=self._session_config.browser_executable,
            )
            return self._adapter_factory(provisioning)

        # Decrypt only here, inside the broker boundary.
        try:
            session_state = self._cipher.decrypt(reference.ciphertext)
        except Exception as error:
            with self._session_factory() as session:
                SessionReferenceRepository(session).record_auth_failure(reference.reference_key)
                session.commit()
            raise SessionBrokerError("failed to decrypt session state") from error

        provisioning = AdapterProvisioning(
            marketplace=marketplace,
            account_id=account_id,
            allowed_domains=list(reference.allowed_domains),
            storage_dir=Path(reference.storage_dir),
            session_state=session_state,
            browser_executable=self._session_config.browser_executable,
        )
        return self._adapter_factory(provisioning)

    def revoke_session(self, account_id: UUID, *, actor: str = "human:operator") -> None:
        storage_dir: Path | None = None
        with self._session_factory() as session:
            references = SessionReferenceRepository(session)
            reference = references.get_active(account_id, now=datetime.now(UTC))
            if reference is not None:
                references.revoke(reference.reference_key)
                storage_dir = Path(reference.storage_dir).resolve()
            accounts = MarketplaceAccountRepository(session)
            accounts.set_session(account_id, reference=None, expires_at=None)
            accounts.set_status(
                account_id, status=MarketplaceAccountStatus.AUTHENTICATION_REQUIRED
            )
            AuditEventRepository(session).append(
                event_type="session.revoked",
                actor_type="human",
                actor_id=actor.partition(":")[2] or actor,
                resource_type="marketplace_account",
                resource_id=account_id,
                details={},
            )
            session.commit()
        if storage_dir is not None:
            root = self._session_root()
            if storage_dir.is_relative_to(root) and storage_dir != root:
                try:
                    storage_dir.rmdir()
                except OSError:
                    pass

    def mark_expired(self, account_id: UUID) -> None:
        with self._session_factory() as session:
            MarketplaceAccountRepository(session).set_status(
                account_id, status=MarketplaceAccountStatus.AUTHENTICATION_REQUIRED
            )
            AuditEventRepository(session).append(
                event_type="session.expired",
                actor_type="system",
                actor_id="session-broker",
                resource_type="marketplace_account",
                resource_id=account_id,
                details={},
            )
            session.commit()
