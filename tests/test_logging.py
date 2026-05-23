"""Tests for the token-scrubbing log filter."""

from __future__ import annotations

import logging

from nightscout_mcp.logging_setup import TokenScrubFilter


def _emit(logger: logging.Logger, msg: str, *args: object) -> str:
    """Run msg/args through the filter and return the rendered message."""
    record = logger.makeRecord(
        name=logger.name,
        level=logging.INFO,
        fn="t",
        lno=1,
        msg=msg,
        args=args if args else None,
        exc_info=None,
    )
    TokenScrubFilter().filter(record)
    return record.getMessage()


def test_scrubs_token_query_param() -> None:
    out = _emit(
        logging.getLogger("t"),
        "GET https://x.test/api/v1/entries.json?token=mcp-reader-deadbeef&count=10",
    )
    assert "deadbeef" not in out
    assert "token=***" in out
    assert "count=10" in out  # other params preserved


def test_scrubs_bearer_header_string() -> None:
    out = _emit(
        logging.getLogger("t"),
        "Authorization: Bearer eyJabc.def.ghi",
    )
    assert "eyJabc.def.ghi" not in out
    assert "Bearer ***" in out


def test_scrubs_url_encoded_token() -> None:
    out = _emit(
        logging.getLogger("t"),
        "Request URL: https://x.test/api/v1/entries.json%3Ftoken%3Dnightscout-reader-abc123",
    )
    assert "abc123" not in out


def test_scrubs_api_secret_hash_header() -> None:
    out = _emit(
        logging.getLogger("t"),
        "api-secret: deadbeefcafe1234567890abcdef",
    )
    assert "deadbeefcafe1234567890abcdef" not in out


def test_passes_through_unrelated_messages_untouched() -> None:
    msg = "GET /api/v1/status.json 200 OK"
    out = _emit(logging.getLogger("t"), msg)
    assert out == msg


def test_handles_args_substitution() -> None:
    # When httpx logs with args, the filter renders + scrubs at emit time.
    out = _emit(
        logging.getLogger("t"),
        "HTTP %s %s",
        "GET",
        "https://x.test/api/v1/entries.json?token=secret-token-value-here",
    )
    assert "secret-token-value-here" not in out
    assert "token=***" in out
