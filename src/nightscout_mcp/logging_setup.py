"""Structured logging + token scrubbing.

When this MCP runs under stdio (the default Claude Desktop / Claude Code
transport), stderr is captured by the parent MCP client and often ends up
in its debug log. httpx's default logging includes the full request URL,
which means a naive setup would leak `?token=...` to disk.

Two layers of defense:

1. **Value-based scrubbing** — the process knows the actual token
   (`Settings.nightscout_token`). Once `register_secret()` has been called
   with it, that literal value (and its URL-encoded forms) is redacted from
   every log line *regardless of shape* — query param, Bearer header, JSON
   body, or a stack trace. This is the reliable layer.
2. **Pattern-based scrubbing** — a regex fallback that redacts token-shaped
   substrings even before a secret is registered (e.g. logs emitted during
   early startup), covering `token=`, `Bearer`, and `api-secret:` forms.

Both are applied by a `logging.Filter` (message text) *and* a `Formatter`
subclass (fully-rendered output, which is the only place an exception
traceback appears — filters run before the traceback is formatted).
"""

from __future__ import annotations

import logging
import re
import sys
from urllib.parse import quote, quote_plus

# Token-shaped patterns. Case-insensitive; broad enough to catch URL-encoded
# and non-hex variants, anchored on the key so unrelated text is untouched.
_TOKEN_PATTERNS = [
    re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE),
    re.compile(r"(token%3D)[^&\s\"']+", re.IGNORECASE),  # URL-encoded =
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"(api-secret[\"']?\s*[:=]\s*[\"']?)[^\s\"',&}]+", re.IGNORECASE),
]

# Known literal secret values (and encoded forms) to redact wherever they appear.
_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a literal secret (e.g. the reader token) for value-based scrubbing.

    Adds the raw value plus its URL-encoded variants so the token is redacted
    even inside a percent-encoded URL or a JSON body. Short/empty values are
    ignored to avoid over-redacting incidental substrings.
    """
    if not value or len(value) < 6:
        return
    for form in (value, quote(value, safe=""), quote_plus(value)):
        _SECRETS.add(form)


def _scrub_text(text: str) -> str:
    """Redact registered secrets (value-based) then token-shaped patterns."""
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, "***")
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(r"\1***", text)
    return text


class TokenScrubFilter(logging.Filter):
    """Scrub secrets from a log record's message before it is handled."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg = _scrub_text(msg)
        record.args = None
        return True


class ScrubbingFormatter(logging.Formatter):
    """Formatter that scrubs the *fully rendered* line — including any exception
    traceback, which a Filter never sees (tracebacks are formatted here, after
    filters have run). This is what stops a `logger.exception(...)` carrying an
    httpx error URL from leaking the token to disk.
    """

    def format(self, record: logging.LogRecord) -> str:
        return _scrub_text(super().format(record))


_CONFIGURED = False


def setup_logging(level: int = logging.INFO) -> None:
    """Idempotent — safe to call multiple times. Installs the scrubbing filter
    and formatter on the root logger and on httpx's own logger.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        ScrubbingFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )

    scrub = TokenScrubFilter()
    handler.addFilter(scrub)

    root = logging.getLogger()
    root.setLevel(level)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        root.addHandler(handler)
    else:
        # Retrofit whatever handler is already there with the scrubbing layer.
        for h in root.handlers:
            h.addFilter(scrub)
            if not isinstance(h.formatter, ScrubbingFormatter):
                h.setFormatter(
                    ScrubbingFormatter(
                        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S",
                    )
                )

    # httpx logs requests with the full URL — filter that logger directly too.
    logging.getLogger("httpx").addFilter(scrub)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Convenience accessor — assumes setup_logging() has been called."""
    return logging.getLogger(name)
