"""Tests for the token-scrubbing log filter."""

from __future__ import annotations

import logging

import pytest

from nightscout_mcp.logging_setup import (
    _SECRETS,
    ScrubbingFormatter,
    TokenScrubFilter,
    register_secret,
)


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


@pytest.fixture
def _registered_secret():
    token = "mcp-reader-canary-XYZ789"
    register_secret(token)
    yield token
    _SECRETS.discard(token)


def test_registered_secret_redacted_in_any_shape(_registered_secret: str) -> None:
    token = _registered_secret
    # Value-based scrubbing catches the token even where no `token=` key exists —
    # e.g. embedded in a JSON body or bare in prose.
    out = _emit(logging.getLogger("t"), f'{{"auth": "{token}"}} used for request')
    assert token not in out
    assert "***" in out


def test_registered_secret_redacted_in_exception_traceback(
    _registered_secret: str,
) -> None:
    """The killer case: a formatted traceback (which a Filter never sees) must
    still have the token stripped by the ScrubbingFormatter."""
    token = _registered_secret
    fmt = ScrubbingFormatter(fmt="%(levelname)s %(message)s")
    try:
        raise RuntimeError(f"Client error for url 'https://x.test/api/v1/entries.json?token={token}'")
    except RuntimeError:
        logger = logging.getLogger("t")
        record = logger.makeRecord("t", logging.ERROR, "f", 1, "boom", None, exc_info=__import__("sys").exc_info())
    rendered = fmt.format(record)
    assert token not in rendered  # traceback text scrubbed
    assert "***" in rendered


def test_bearer_scrub_is_case_insensitive() -> None:
    out = _emit(logging.getLogger("t"), "authorization: bearer eyJlow.case.jwt")
    assert "eyJlow.case.jwt" not in out


def test_non_hex_api_secret_scrubbed() -> None:
    out = _emit(logging.getLogger("t"), "api-secret: NotHexButStillSecret!123")
    assert "NotHexButStillSecret" not in out
