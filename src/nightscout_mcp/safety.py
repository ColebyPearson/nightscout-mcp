"""Cross-cutting safety guards.

Phase 0 ships only the https-only check (also enforced in config.py). More
guards land alongside the write tools in Phase 3 (range clamps, confirmation
phrases, audit logging).
"""

from __future__ import annotations


class UnsafeConfigError(RuntimeError):
    """Raised when configuration would let us do something unsafe."""


def assert_https(url: str) -> None:
    """Belt-and-braces check; config.py's validator is the primary gate."""
    if not url.startswith("https://"):
        raise UnsafeConfigError(
            f"NIGHTSCOUT_URL must use https:// (got: {url}). "
            "Refusing to send a token over cleartext."
        )
