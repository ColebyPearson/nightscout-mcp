"""FastMCP server entry point.

Phase 0 exposes a single tool — `health_check` — so the wiring can be verified
end-to-end (env -> config -> httpx -> Nightscout -> back through MCP) before
the Phase 1 read tools land.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __version__
from .client import NightscoutClient
from .config import Settings, load_settings

# Module-level singletons. Loaded lazily so `import` doesn't require a valid
# .env (helpful for tests and for `uv run mcp dev` introspection).
_settings: Settings | None = None
_client: NightscoutClient | None = None


def _get_client() -> NightscoutClient:
    global _settings, _client
    if _client is None:
        _settings = load_settings()
        _client = NightscoutClient(_settings)
    return _client


mcp = FastMCP("nightscout-mcp")


@mcp.tool()
async def health_check() -> dict[str, Any]:
    """Verify the MCP can reach your Nightscout instance.

    Returns the Nightscout server version, configured units, and the MCP
    version. Use this first after configuring .env to confirm everything is
    wired correctly. No glucose data is returned.
    """
    client = _get_client()
    status = await client.status()
    assert _settings is not None  # set by _get_client
    return {
        "mcp_version": __version__,
        "mcp_default_units": _settings.nightscout_units,
        "nightscout_url": _settings.base_url,
        "nightscout_version": status.get("version"),
        "nightscout_status": status.get("status"),
        "nightscout_server_units": status.get("settings", {}).get("units"),
        "ok": True,
    }


def main() -> None:
    """Console entry point — runs the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
