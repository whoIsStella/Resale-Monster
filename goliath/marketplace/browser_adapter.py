"""Playwright-based browser adapter foundation and marketplace skeletons.

The foundation manages an isolated, authenticated browser context created from a
decrypted storage-state file inside the adapter boundary. Playwright is imported
lazily so the package imports without it and tests never launch a browser or
touch the network. Marketplace-specific selectors and workflows live in each
subclass; the base never guesses which button means "publish".

No CAPTCHA/2FA/anti-bot bypass, no rate-limit circumvention, no use of the user's
normal browser profile.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from goliath.marketplace.adapter import BaseMarketplaceAdapter, OperationResult


class BrowserAdapterError(RuntimeError):
    pass


class PlaywrightBrowserAdapter(BaseMarketplaceAdapter):
    """Foundation for Playwright adapters. Concrete write ops live in subclasses."""

    marketplace = "browser"

    def __init__(
        self,
        *,
        storage_state_path: Path | None = None,
        storage_state: bytes | None = None,
        allowed_domains: list[str],
        browser_executable: str | None = None,
        headless: bool = True,
    ) -> None:
        # The storage-state file holds the decrypted session; it stays private to
        # this adapter and is never returned to callers or logged.
        if storage_state_path is None and storage_state is None:
            raise BrowserAdapterError("encrypted session state is required")
        self._storage_state_path = storage_state_path
        self._storage_state = bytes(storage_state or b"")
        self._allowed_domains = list(allowed_domains)
        self._browser_executable = browser_executable
        self._headless = headless
        self._context = None

    def capabilities(self) -> set[str]:
        # A bare foundation declares only reads until a subclass adds writes.
        return {"authentication_status"}

    def _require_playwright(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError as error:  # pragma: no cover - depends on optional install
            raise BrowserAdapterError(
                "Playwright is not installed; install it to use browser adapters"
            ) from error
        return async_playwright

    def _guard_domain(self, url: str) -> None:
        host = url.split("://", 1)[-1].split("/", 1)[0].lower()
        if not any(host == d or host.endswith("." + d) for d in self._allowed_domains):
            raise BrowserAdapterError(f"navigation to disallowed domain blocked: {host}")

    async def authentication_status(self) -> OperationResult:
        # Only report presence. Never return or log storage state.
        exists = bool(self._storage_state) or bool(
            self._storage_state_path
            and self._storage_state_path.exists()
            and self._storage_state_path.stat().st_size > 0
        )
        return OperationResult(
            ok=True, operation="authentication_status", data={"authenticated": bool(exists)}
        )

    def _decoded_storage_state(self) -> dict[str, Any]:
        """Decode session state inside the adapter boundary without persisting plaintext."""
        raw = self._storage_state
        if not raw and self._storage_state_path is not None:
            raw = self._storage_state_path.read_bytes()
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BrowserAdapterError("invalid Playwright storage state") from error
        if not isinstance(decoded, dict):
            raise BrowserAdapterError("invalid Playwright storage state")
        return decoded

    async def open_context(self):
        """Start an isolated context; subclasses use this for marketplace workflows."""
        playwright = await self._require_playwright()().start()
        launch: dict[str, Any] = {"headless": self._headless}
        if self._browser_executable:
            launch["executable_path"] = self._browser_executable
        browser = await playwright.chromium.launch(**launch)
        self._context = await browser.new_context(storage_state=self._decoded_storage_state())
        async def enforce_domain(route):
            url = route.request.url
            if url.startswith(("about:", "data:", "blob:")):
                await route.continue_()
                return
            try:
                self._guard_domain(url)
            except BrowserAdapterError:
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        await self._context.route("**/*", enforce_domain)
        return self._context

    async def close(self) -> None:
        """Safe browser shutdown; releases the context if one was opened."""
        if self._context is not None:  # pragma: no cover - requires a live browser
            browser = self._context.browser
            await self._context.close()
            self._context = None
            if browser is not None:
                await browser.close()


class _MarketplaceSkeleton(PlaywrightBrowserAdapter):
    """Shared skeleton: declares reads only; write ops remain unimplemented."""

    def capabilities(self) -> set[str]:
        return {"authentication_status", "read_account", "sync_account"}

    async def read_account(self) -> OperationResult:
        return OperationResult(
            ok=True, operation="read_account", data={"marketplace": self.marketplace}
        )

    async def sync_account(self) -> OperationResult:
        # Real implementations read the marketplace via the isolated context.
        return OperationResult(
            ok=True, operation="sync_account", data={"marketplace": self.marketplace, "synced": 0}
        )


class EbayBrowserAdapter(_MarketplaceSkeleton):
    marketplace = "ebay"


class PoshmarkBrowserAdapter(_MarketplaceSkeleton):
    marketplace = "poshmark"


class DepopBrowserAdapter(_MarketplaceSkeleton):
    marketplace = "depop"


class MercariBrowserAdapter(_MarketplaceSkeleton):
    marketplace = "mercari"


class GrailedBrowserAdapter(_MarketplaceSkeleton):
    marketplace = "grailed"


class FacebookBrowserAdapter(_MarketplaceSkeleton):
    marketplace = "facebook"


BROWSER_ADAPTERS: dict[str, type[_MarketplaceSkeleton]] = {
    "ebay": EbayBrowserAdapter,
    "poshmark": PoshmarkBrowserAdapter,
    "depop": DepopBrowserAdapter,
    "mercari": MercariBrowserAdapter,
    "grailed": GrailedBrowserAdapter,
    "facebook": FacebookBrowserAdapter,
}
