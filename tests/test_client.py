"""Client-level tests with respx-mocked HTTP. No network."""

from __future__ import annotations

import httpx
import pytest
import respx

from nightscout_mcp.client import NightscoutClient
from nightscout_mcp.config import Settings


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Build deterministic test settings — ignore any real .env in cwd."""
    monkeypatch.setenv("NIGHTSCOUT_URL", "https://example.nightscout.test")
    monkeypatch.setenv("NIGHTSCOUT_TOKEN", "mcp-reader-abc12345")
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.asyncio
@respx.mock
async def test_status_injects_token(settings: Settings) -> None:
    route = respx.get("https://example.nightscout.test/api/v1/status.json").mock(
        return_value=httpx.Response(
            200, json={"version": "15.0.7", "status": "ok", "settings": {"units": "mmol"}}
        )
    )
    client = NightscoutClient(settings)
    try:
        data = await client.status()
    finally:
        await client.aclose()

    assert data["version"] == "15.0.7"
    # Token must be in the query string, never in a header.
    assert route.calls.last.request.url.params["token"] == "mcp-reader-abc12345"
    assert "api-secret" not in route.calls.last.request.headers


@pytest.mark.asyncio
@respx.mock
async def test_get_raises_on_4xx(settings: Settings) -> None:
    respx.get("https://example.nightscout.test/api/v1/entries.json").mock(
        return_value=httpx.Response(401, json={"status": 401, "message": "Unauthorized"})
    )
    client = NightscoutClient(settings)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get("/api/v1/entries.json")
    finally:
        await client.aclose()
