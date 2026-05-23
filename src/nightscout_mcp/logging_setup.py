"""Structured logging + token scrubbing.

When this MCP runs under stdio (the default Claude Desktop / Claude Code
transport), stderr is captured by the parent MCP client and often ends up
in its debug log. httpx's default logging includes the full request URL,
which means a naive setup would leak `?token=...` to disk.

This module installs a logging.Filter on every relevant logger that
replaces token values with `***`. The filter is regex-based so it catches
the token regardless of where in a log message it appears.
"""

from __future__ import annotations

import logging
import re
import sys

# Match Nightscout token query params and Authorization headers.
# Greedy enough to catch URL-encoded variants but anchored so we don't
# clobber unrelated `token` strings (e.g. JWT chunks in payloads).
_TOKEN_PATTERNS = [
    re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE),
    re.compile(r"(token%3D)[^&\s\"']+", re.IGNORECASE),  # URL-encoded =
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(api-secret:\s*)[A-Fa-f0-9]+", re.IGNORECASE),
]


class TokenScrubFilter(logging.Filter):
    """Replace token values in log messages with `***`."""

    def filter(self, record: logging.LogRecord) -> bool:
        # The httpx logger renders args into the message at emit time.
        # Render eagerly, then scrub the rendered message, so we cover
        # both pre- and post-formatting cases.
        try:
            msg = record.getMessage()
        except Exception:
            # If formatting fails, fall back to the raw msg attribute.
            msg = str(record.msg)

        for pattern in _TOKEN_PATTERNS:
            msg = pattern.sub(r"\1***", msg)

        # Replace the record's msg/args so downstream handlers see scrubbed text.
        record.msg = msg
        record.args = None
        return True


_CONFIGURED = False


def setup_logging(level: int = logging.INFO) -> None:
    """Idempotent — safe to call multiple times. Installs the scrub filter
    on the root logger and on httpx's own logger.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )

    scrub = TokenScrubFilter()
    handler.addFilter(scrub)

    root = logging.getLogger()
    root.setLevel(level)
    # Don't double-attach if something else has already configured logging.
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        root.addHandler(handler)
    else:
        # Still attach the filter to whatever handler is already there.
        for h in root.handlers:
            h.addFilter(scrub)

    # httpx logs requests with the full URL — make sure the filter is on
    # that logger too, even if it bypasses the root.
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.addFilter(scrub)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Convenience accessor — assumes setup_logging() has been called."""
    return logging.getLogger(name)
