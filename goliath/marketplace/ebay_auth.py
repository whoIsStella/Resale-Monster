"""eBay OAuth lifecycle inside the marketplace credential boundary."""

from __future__ import annotations

import base64
import json
import os
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx
from sqlalchemy.orm import Session, sessionmaker

from goliath.config import OrchestrationConfig
from goliath.db.marketplace_repositories import MarketplaceAccountRepository, OAuthStateRepository
from goliath.db.models import MarketplaceAccountStatus, utc_now
from goliath.db.repositories import AuditEventRepository
from goliath.marketplace.broker import SessionBroker
from goliath.marketplace.ebay import (
    EBAY_PRODUCTION_API,
    EBAY_SANDBOX_API,
    EbayAdapterSettings,
    EbayMarketplaceAdapter,
    EbayTransport,
)


class EbayOAuthError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EbayOAuthCredential:
    access_token: str
    refresh_token: str
    access_expires_at: str
    refresh_expires_at: str | None
    scopes: list[str]
    token_type: str = "User Access Token"

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True).encode()

    @classmethod
    def from_bytes(cls, value: bytes) -> EbayOAuthCredential:
        try:
            raw = json.loads(value)
            credential = cls(**raw)
            if not credential.access_token or not credential.refresh_token:
                raise ValueError
            datetime.fromisoformat(credential.access_expires_at)
            if credential.refresh_expires_at:
                datetime.fromisoformat(credential.refresh_expires_at)
            return credential
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise EbayOAuthError("invalid encrypted eBay credential") from error

    def access_expiration(self) -> datetime:
        value = datetime.fromisoformat(self.access_expires_at)
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class EbayOAuthService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        config: OrchestrationConfig,
        broker: SessionBroker,
        client: httpx.AsyncClient | None = None,
        identity_transport_factory: Callable[[EbayOAuthCredential, bool], Any] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._oauth = config.marketplace.ebay_oauth
        self._broker = broker
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(config.marketplace.transport.total_timeout_seconds),
            verify=True,
            follow_redirects=False,
        )
        self._identity_transport_factory = identity_transport_factory

    def initiate(self, account_id: UUID, *, sandbox: bool) -> dict[str, Any]:
        if not self._oauth.enabled or not self._oauth.redirect_uri:
            raise EbayOAuthError("eBay OAuth is not configured")
        client_id, _ = self._client_credentials()
        state = secrets.token_urlsafe(32)
        expires_at = utc_now() + timedelta(seconds=self._oauth.state_ttl_seconds)
        with self._session_factory() as session:
            account = MarketplaceAccountRepository(session).require(account_id)
            if account.marketplace.value != "ebay":
                raise EbayOAuthError("OAuth account marketplace mismatch")
            OAuthStateRepository(session).create(
                account_id=account_id,
                state=state,
                redirect_uri=self._oauth.redirect_uri,
                requested_scopes=list(self._oauth.scopes),
                expires_at=expires_at,
            )
            MarketplaceAccountRepository(session).set_status(
                account_id, status=MarketplaceAccountStatus.CONNECTING
            )
            AuditEventRepository(session).append(
                event_type="marketplace.oauth.initiated",
                actor_type="human",
                actor_id="operator",
                resource_type="marketplace_account",
                resource_id=account_id,
                details={"sandbox": sandbox, "expires_at": expires_at.isoformat()},
            )
            session.commit()
        auth_url = (
            self._oauth.sandbox_authorization_url
            if sandbox
            else self._oauth.production_authorization_url
        )
        query = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": self._oauth.redirect_uri,
                "response_type": "code",
                "scope": " ".join(self._oauth.scopes),
                "state": state,
            }
        )
        return {
            "authorization_url": f"{auth_url}?{query}",
            "state": state,
            "expires_at": expires_at.isoformat(),
            "sandbox": sandbox,
        }

    async def callback(self, *, code: str, state: str, sandbox: bool) -> dict[str, Any]:
        if not code or len(code) > 4096 or not state:
            raise EbayOAuthError("invalid OAuth callback")
        redirect_uri = self._oauth.redirect_uri
        if not redirect_uri:
            raise EbayOAuthError("eBay OAuth is not configured")
        with self._session_factory() as session:
            record = OAuthStateRepository(session).consume(state=state, redirect_uri=redirect_uri)
            account_id = record.account_id
            requested_scopes = set(record.requested_scopes)
            session.commit()
        token = await self._token_request(
            sandbox=sandbox,
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        )
        credential = self._credential_from_token(token, prior_refresh=None)
        if not requested_scopes.issubset(set(credential.scopes)):
            self._set_reauthentication(account_id, "required OAuth scopes were not granted")
            raise EbayOAuthError("eBay did not grant all required scopes")
        identity = await self._validate_identity(account_id, credential, sandbox=sandbox)
        self._broker.store_session(
            account_id, credential.to_bytes(), allowed_domains=["ebay.com"], actor="human:oauth"
        )
        with self._session_factory() as session:
            account = MarketplaceAccountRepository(session).require(account_id)
            account.remote_account_identifier = identity["remote_account_id"]
            account.display_seller_name = identity["seller_name"]
            account.granted_scopes = credential.scopes
            account.credential_version = (account.credential_version or 0) + 1
            account.authentication_expires_at = credential.access_expiration()
            account.last_authentication_check_at = utc_now()
            account.status = MarketplaceAccountStatus.ACTIVE
            account.version += 1
            AuditEventRepository(session).append(
                event_type="marketplace.oauth.connected",
                actor_type="human",
                actor_id="oauth",
                resource_type="marketplace_account",
                resource_id=account_id,
                details={
                    "credential_version": account.credential_version,
                    "scope_count": len(credential.scopes),
                    "sandbox": sandbox,
                },
            )
            credential_version = account.credential_version
            session.commit()
        return {
            "account_id": str(account_id),
            "connected": True,
            "seller_name": identity["seller_name"],
            "scopes": credential.scopes,
            "credential_version": credential_version,
        }

    async def refresh(self, account_id: UUID, *, sandbox: bool) -> dict[str, Any]:
        credential = self._broker.read_ebay_credential(account_id)
        token = await self._token_request(
            sandbox=sandbox,
            data={
                "grant_type": "refresh_token",
                "refresh_token": credential.refresh_token,
                "scope": " ".join(credential.scopes),
            },
        )
        rotated = self._credential_from_token(token, prior_refresh=credential)
        identity = await self._validate_identity(account_id, rotated, sandbox=sandbox)
        self._broker.store_session(
            account_id,
            rotated.to_bytes(),
            allowed_domains=["ebay.com"],
            actor="system:token-refresh",
        )
        with self._session_factory() as session:
            account = MarketplaceAccountRepository(session).require(account_id)
            account.credential_version = (account.credential_version or 0) + 1
            account.authentication_expires_at = rotated.access_expiration()
            account.last_authentication_check_at = utc_now()
            account.status = MarketplaceAccountStatus.ACTIVE
            account.version += 1
            AuditEventRepository(session).append(
                event_type="marketplace.oauth.refreshed",
                actor_type="system",
                actor_id="credential-broker",
                resource_type="marketplace_account",
                resource_id=account_id,
                details={"credential_version": account.credential_version},
            )
            credential_version = account.credential_version
            session.commit()
        return {
            "account_id": str(account_id),
            "refreshed": True,
            "seller_name": identity["seller_name"],
            "credential_version": credential_version,
        }

    async def revoke(self, account_id: UUID) -> None:
        # Local revocation is immediate. Remote revocation can be performed through
        # eBay's consent UI; no token is returned to this method or its caller.
        self._broker.revoke_session(account_id, actor="human:operator")
        with self._session_factory() as session:
            account = MarketplaceAccountRepository(session).require(account_id)
            account.status = MarketplaceAccountStatus.REVOKED
            account.granted_scopes = []
            account.version += 1
            session.commit()

    async def _validate_identity(
        self, account_id: UUID, credential: EbayOAuthCredential, *, sandbox: bool
    ) -> dict[str, str]:
        settings = EbayAdapterSettings()
        transport = (
            self._identity_transport_factory(credential, sandbox)
            if self._identity_transport_factory
            else EbayTransport(
                access_token=credential.access_token,
                base_url=EBAY_SANDBOX_API if sandbox else EBAY_PRODUCTION_API,
            )
        )
        try:
            result = await EbayMarketplaceAdapter(
                transport=transport, settings=settings
            ).read_account()
        finally:
            close = getattr(transport, "close", None)
            if close is not None:
                await close()
        if not result.ok:
            raise EbayOAuthError("eBay account identity validation failed")
        with self._session_factory() as session:
            account = MarketplaceAccountRepository(session).require(account_id)
            prior = account.remote_account_identifier
        if prior and prior != result.data["remote_account_id"]:
            self._set_reauthentication(account_id, "eBay account identity mismatch")
            raise EbayOAuthError("eBay account identity mismatch")
        return {
            "remote_account_id": str(result.data["remote_account_id"]),
            "seller_name": str(result.data["seller_name"]),
        }

    async def _token_request(self, *, sandbox: bool, data: dict[str, str]) -> dict[str, Any]:
        client_id, client_secret = self._client_credentials()
        token_url = self._oauth.sandbox_token_url if sandbox else self._oauth.production_token_url
        allowed = {
            "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
            "https://api.ebay.com/identity/v1/oauth2/token",
        }
        if token_url not in allowed:
            raise EbayOAuthError("eBay token endpoint is not allowlisted")
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        try:
            response = await self._client.post(
                token_url,
                data=data,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise EbayOAuthError("eBay token service unavailable") from error
        if response.status_code != 200:
            raise EbayOAuthError(f"eBay token exchange failed with HTTP {response.status_code}")
        if len(response.content) > 100_000 or "application/json" not in response.headers.get(
            "content-type", ""
        ):
            raise EbayOAuthError("invalid eBay token response")
        try:
            value = response.json()
        except json.JSONDecodeError as error:
            raise EbayOAuthError("invalid eBay token response") from error
        if not isinstance(value, dict):
            raise EbayOAuthError("invalid eBay token response")
        return value

    def _credential_from_token(
        self, value: dict[str, Any], *, prior_refresh: EbayOAuthCredential | None
    ) -> EbayOAuthCredential:
        try:
            access = str(value["access_token"])
            refresh = str(
                value.get("refresh_token") or (prior_refresh.refresh_token if prior_refresh else "")
            )
            expires = int(value["expires_in"])
            refresh_expires = value.get("refresh_token_expires_in")
            scopes = str(value.get("scope", "")).split() or (
                prior_refresh.scopes if prior_refresh else []
            )
            if not access or not refresh or expires <= 0 or not scopes:
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise EbayOAuthError("eBay token response omitted required fields") from error
        now = utc_now()
        return EbayOAuthCredential(
            access_token=access,
            refresh_token=refresh,
            access_expires_at=(now + timedelta(seconds=expires)).isoformat(),
            refresh_expires_at=(now + timedelta(seconds=int(refresh_expires))).isoformat()
            if refresh_expires
            else (prior_refresh.refresh_expires_at if prior_refresh else None),
            scopes=scopes,
        )

    def _client_credentials(self) -> tuple[str, str]:
        client_id = os.environ.get(self._oauth.client_id_env, "")
        client_secret = os.environ.get(self._oauth.client_secret_env, "")
        if not client_id or not client_secret:
            raise EbayOAuthError("eBay client credentials are unavailable")
        return client_id, client_secret

    def _set_reauthentication(self, account_id: UUID, reason: str) -> None:
        with self._session_factory() as session:
            account = MarketplaceAccountRepository(session).require(account_id)
            account.status = MarketplaceAccountStatus.AUTHENTICATION_REQUIRED
            account.degradation_reason = reason
            account.last_failed_authentication_check_at = utc_now()
            account.version += 1
            session.commit()
