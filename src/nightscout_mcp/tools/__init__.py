"""MCP tool registration.

Each module exposes a `register(mcp, get_client)` function so tools can
be wired into the FastMCP instance from server.py without circular
imports. Tests can call register() with a mock client.
"""
