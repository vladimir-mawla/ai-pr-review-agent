"""Application configuration.

Owns: reading configuration from environment variables (and an optional local
``.env`` file for development) into a single typed, validated settings object.
Nothing else in the codebase should read ``os.environ`` directly for these
values — this module is the one source of truth, which is why it lives in
``backend.core``: per ADR-002 every other layer may depend on it, and it must
never import outward to any of them.

Why pydantic-settings: it gives us validation (a missing/blank secret fails
fast at startup instead of producing a validator that always rejects real
signatures) and a documented, typed surface instead of scattered
``os.environ.get(...)`` calls with silent string defaults.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the pr-review-agent backend.

    Attributes:
        github_webhook_secret: The shared secret GitHub signs webhook request
            bodies with (HMAC-SHA256). Required, never hardcoded, and never
            given a default value here — a default would mean a misconfigured
            deployment silently accepts a well-known secret instead of
            failing to start. See ``.env.example`` for the documented
            environment variable name and local-dev instructions.
    """

    github_webhook_secret: str = Field(
        min_length=1,
        description=(
            "Shared secret used to verify GitHub webhook HMAC-SHA256 "
            "signatures. Set via the GITHUB_WEBHOOK_SECRET environment "
            "variable or a local .env file; never commit a real value."
        ),
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings singleton, built once and cached.

    Cached (rather than re-read per call) because environment configuration
    does not change within a process lifetime, and re-parsing on every
    request would be pure overhead in the webhook request path.
    """
    # mypy sees `github_webhook_secret` as a required constructor argument
    # because it has no default; at runtime pydantic-settings populates it
    # from the environment / .env file instead, which the type checker has
    # no way to see. This is the standard, documented false positive for
    # pydantic-settings with required fields.
    return Settings()  # type: ignore[call-arg]
