"""Configuration loaded from environment / .env.

We refuse to boot if the URL isn't https:// or the token is missing — the whole
point of the MCP is to talk to a real Nightscout instance over an authenticated
channel, so failing fast is safer than starting up in a broken state and only
noticing on the first tool call.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide configuration. Read once at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    nightscout_url: HttpUrl = Field(
        ...,
        description="Your Nightscout instance URL. Must be https://.",
    )
    nightscout_token: str = Field(
        ...,
        min_length=8,
        description="A readable-role access token from Admin Tools -> Subjects.",
    )
    nightscout_units: Literal["mmol/L", "mg/dL"] = Field(
        default="mmol/L",
        description="Default units for LLM-friendly summaries. Raw payloads include both.",
    )

    # Phase 3 (writes) — present for forward compatibility; not used yet.
    nightscout_allow_writes: bool = Field(default=False)
    nightscout_writer_token: str | None = Field(default=None)

    @field_validator("nightscout_url")
    @classmethod
    def _must_be_https(cls, v: HttpUrl) -> HttpUrl:
        if v.scheme != "https":
            # Health data over plaintext is non-negotiable.
            raise ValueError(
                f"NIGHTSCOUT_URL must use https:// (got {v.scheme}://). "
                "Cleartext HTTP is unsafe for health data."
            )
        return v

    @property
    def base_url(self) -> str:
        """Canonical base URL without trailing slash."""
        return str(self.nightscout_url).rstrip("/")


def load_settings() -> Settings:
    """Load and validate settings. Raises ValidationError on bad config."""
    return Settings()  # type: ignore[call-arg]
