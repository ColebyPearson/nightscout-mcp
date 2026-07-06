"""Client-level tests with respx-mocked HTTP. No network."""

from __future__ import annotations

import logging

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
        return_value=httpx.Response(200, json={"version": "15.0.7", "status": "ok", "settings": {"units": "mmol"}})
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
async def test_get_raises_sanitized_error_on_4xx(settings: Settings) -> None:
    """A failed request must raise a token-free error.

    httpx.HTTPStatusError embeds the full request URL (incl. ?token=...) in its
    message, and FastMCP surfaces str(exc) back to the LLM on tool failure. The
    client must catch and re-raise without the token or the raw URL.
    """
    respx.get("https://example.nightscout.test/api/v1/entries.json").mock(
        return_value=httpx.Response(401, json={"status": 401, "message": "Unauthorized"})
    )
    client = NightscoutClient(settings)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await client.get("/api/v1/entries.json")
    finally:
        await client.aclose()

    msg = str(excinfo.value)
    assert "mcp-reader-abc12345" not in msg  # token must never leak
    assert "token" not in msg
    assert "401" in msg


@pytest.mark.asyncio
@respx.mock
async def test_get_raises_sanitized_error_on_transport_failure(
    settings: Settings,
) -> None:
    """Connect/timeout errors must also be sanitized — httpx can stringify the
    URL (with token) in RequestError messages too."""
    respx.get("https://example.nightscout.test/api/v1/entries.json").mock(side_effect=httpx.ConnectTimeout("timed out"))
    client = NightscoutClient(settings)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await client.get("/api/v1/entries.json")
    finally:
        await client.aclose()

    msg = str(excinfo.value)
    assert "mcp-reader-abc12345" not in msg
    assert "token" not in msg


def test_client_construction_attaches_scrub_filter_to_httpx_logger(
    settings: Settings,
) -> None:
    """Issue #3: even consumers that bypass setup_logging() (tests, MCP
    Inspector, custom embeddings) get scrubbed httpx request logs."""
    from nightscout_mcp.logging_setup import TokenScrubFilter

    httpx_logger = logging.getLogger("httpx")
    # Remove any existing scrub filter to prove construction adds one.
    for f in list(httpx_logger.filters):
        if isinstance(f, TokenScrubFilter):
            httpx_logger.removeFilter(f)

    NightscoutClient(settings)
    assert any(isinstance(f, TokenScrubFilter) for f in httpx_logger.filters)


def test_client_construction_does_not_duplicate_scrub_filter(
    settings: Settings,
) -> None:
    """Calling NightscoutClient(...) repeatedly must not pile filters."""
    from nightscout_mcp.logging_setup import TokenScrubFilter

    NightscoutClient(settings)
    NightscoutClient(settings)
    NightscoutClient(settings)
    httpx_logger = logging.getLogger("httpx")
    count = sum(1 for f in httpx_logger.filters if isinstance(f, TokenScrubFilter))
    assert count == 1
