"""FastMCP server entry point.

The FastMCP instance is created here, then tool modules attach via their
own `register(mcp, get_client)` functions. This keeps tools dependency-
injectable for tests and avoids circular imports.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .client import NightscoutClient
from .config import Settings, load_settings
from .logging_setup import setup_logging
from .tools import read as read_tools

# Module-level singletons. Loaded lazily so `import` doesn't require a valid
# .env (helpful for tests and for `uv run mcp dev` introspection).
_settings: Settings | None = None
_client: NightscoutClient | None = None


def _get_client() -> NightscoutClient:
    """Lazy singleton — first call validates config and constructs the client."""
    global _settings, _client
    if _client is None:
        _settings = load_settings()
        _client = NightscoutClient(_settings)
    return _client


mcp = FastMCP("nightscout-mcp")
read_tools.register(mcp, _get_client)


def main() -> None:
    """Console entry point — runs the MCP server over stdio."""
    setup_logging()
    mcp.run()


if __name__ == "__main__":
    main()
