"""Thin async wrapper around the Nightscout REST API v1.

We use httpx directly rather than depending on `py-nightscout` (last release
Dec 2021) so we can hit both v1 and v3 from one client when v3 lands later.

The token is sent as a `?token=...` query parameter — never in a header,
never returned to the LLM.
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import httpx

from .config import Settings
from .logging_setup import TokenScrubFilter, register_secret

# Be polite to free-tier Heroku/Atlas Nightscout instances.
_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)

try:
    _VERSION = version("nightscout-mcp")
except PackageNotFoundError:  # not installed (e.g. running from a raw checkout)
    _VERSION = "0+unknown"
_USER_AGENT = f"nightscout-mcp/{_VERSION} (+https://github.com/ColebyPearson/nightscout-mcp)"


def _ensure_httpx_logger_scrubbed() -> None:
    """Attach the token scrub filter to httpx's request logger.

    Idempotent — we check whether a TokenScrubFilter is already attached
    so repeated NightscoutClient construction (e.g. in tests) doesn't pile
    on duplicates. This runs regardless of whether `setup_logging()` was
    called, so even consumers that import nightscout_mcp without going
    through `main()` (tests, MCP Inspector, custom embeddings) still get
    scrubbed logs. See issue #3.
    """
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(f, TokenScrubFilter) for f in httpx_logger.filters):
        httpx_logger.addFilter(TokenScrubFilter())


class NightscoutClient:
    """Lightweight async client. One instance per server process."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Value-based log scrubbing: redact this token wherever it appears in
        # logs (incl. tracebacks / JSON bodies), not just token-shaped patterns.
        register_secret(settings.nightscout_token)
        _ensure_httpx_logger_scrubbed()
        # verify=True keeps default certifi verification; a CA-bundle path
        # supports self-hosted instances behind a private CA (and the dev mock).
        # This never disables verification.
        verify: str | bool = settings.nightscout_ca_bundle or True
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=_DEFAULT_TIMEOUT,
            verify=verify,
            headers={"User-Agent": _USER_AGENT},
        )

    @property
    def base_url(self) -> str:
        """Exposed for tool responses — never includes the token."""
        return self._settings.base_url

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a Nightscout v1 path (e.g. '/api/v1/entries.json'). Returns parsed JSON.

        The reader token is injected automatically. Caller's params win on conflict,
        which is fine — the token name 'token' isn't used by any v1 endpoint.
        """
        merged: dict[str, Any] = {"token": self._settings.nightscout_token}
        if params:
            merged.update(params)
        try:
            resp = await self._http.get(path, params=merged)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            # httpx embeds the full request URL — including ?token=... — in the
            # exception message. FastMCP surfaces str(exc) back to the LLM on a
            # failed tool call, so re-raise with the path only. Never include
            # exc, resp.url, or the params here.
            raise RuntimeError(f"Nightscout returned HTTP {exc.response.status_code} for {path}") from None
        except httpx.RequestError as exc:
            # Connect/timeout/transport errors also stringify the URL with the
            # token in some httpx paths. Report the error class and path only.
            raise RuntimeError(f"Nightscout request failed for {path}: {type(exc).__name__}") from None

    async def status(self) -> dict[str, Any]:
        """Probe /api/v1/status.json — used as a connectivity smoke test."""
        return await self.get("/api/v1/status.json")
