"""Thin async wrapper around the Nightscout REST API v1.

We use httpx directly rather than depending on `py-nightscout` (last release
Dec 2021) so we can hit both v1 and v3 from one client when v3 lands later.

The token is sent as a `?token=...` query parameter — never in a header,
never returned to the LLM.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import Settings

# Be polite to free-tier Heroku/Atlas Nightscout instances.
_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)


class NightscoutClient:
    """Lightweight async client. One instance per server process."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=_DEFAULT_TIMEOUT,
            headers={"User-Agent": "nightscout-mcp/0.1.0 (+https://github.com)"},
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
        resp = await self._http.get(path, params=merged)
        resp.raise_for_status()
        return resp.json()

    async def status(self) -> dict[str, Any]:
        """Probe /api/v1/status.json — used as a connectivity smoke test."""
        return await self.get("/api/v1/status.json")
