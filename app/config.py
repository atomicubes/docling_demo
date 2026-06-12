"""Environment-driven configuration. No magic, no framework."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "DATABASE_URL",
            "postgresql://log2poa:log2poa@localhost:5432/log2poa",
        )
    )
    # Trigram similarity threshold for the fuzzy fallback leg.
    fuzzy_threshold: float = field(
        default_factory=lambda: _env_float("FUZZY_THRESHOLD", 0.60)
    )
    # Below this aggregate confidence the system abstains.
    abstain_threshold: float = field(
        default_factory=lambda: _env_float("ABSTAIN_THRESHOLD", 0.45)
    )
    # Optional LLM presentation layer. Off unless a key is present.
    anthropic_api_key: str = field(
        default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", "")
    )
    anthropic_model: str = field(
        default_factory=lambda: os.environ.get(
            "ANTHROPIC_MODEL", "claude-sonnet-4-6"
        )
    )
    max_upload_bytes: int = field(
        default_factory=lambda: int(os.environ.get("MAX_UPLOAD_BYTES", 5_000_000))
    )


settings = Settings()
