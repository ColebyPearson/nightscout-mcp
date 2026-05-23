"""Unit tests for config validation. No network.

We pass `_env_file=None` everywhere so tests are deterministic regardless of
whether a real .env exists in the cwd (it does, in development).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nightscout_mcp.config import Settings


def _env(**overrides: str) -> dict[str, str]:
    """Build a minimum-valid env dict, then apply overrides."""
    base = {
        "NIGHTSCOUT_URL": "https://example.nightscout.test",
        "NIGHTSCOUT_TOKEN": "mcp-reader-abc12345",
    }
    base.update(overrides)
    return base


def _make(**env_overrides: str) -> Settings:
    """Construct Settings with .env loading disabled."""
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_minimal_valid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    s = _make()
    assert s.base_url == "https://example.nightscout.test"
    assert s.nightscout_units == "mmol/L"  # default
    assert s.nightscout_allow_writes is False


def test_refuses_http_url(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _env(NIGHTSCOUT_URL="http://example.nightscout.test").items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError, match="https"):
        _make()


def test_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("NIGHTSCOUT_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        _make()


def test_rejects_short_token(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _env(NIGHTSCOUT_TOKEN="short").items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError):
        _make()


def test_units_must_be_known(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _env(NIGHTSCOUT_UNITS="mmol").items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError):
        _make()


def test_base_url_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _env(NIGHTSCOUT_URL="https://example.nightscout.test/").items():
        monkeypatch.setenv(k, v)
    s = _make()
    assert s.base_url == "https://example.nightscout.test"
