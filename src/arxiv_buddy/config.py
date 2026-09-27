"""Configuration, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_SMTP_PORT = 587


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from a .env file without overriding the real environment."""
    path = path or Path.cwd() / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set (put it in your environment or .env file)"
        )
    return value


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    from_address: str
    from_name: str
    use_ssl: bool
    use_starttls: bool

    @classmethod
    def from_env(cls) -> SmtpConfig:
        host = _require("SMTP_HOST")
        raw_port = os.environ.get("SMTP_PORT", "").strip()
        try:
            port = int(raw_port) if raw_port else DEFAULT_SMTP_PORT
        except ValueError as exc:
            raise ConfigError(f"SMTP_PORT must be a number, got {raw_port!r}") from exc

        username = _require("SMTP_USERNAME")
        password = _require("SMTP_PASSWORD")
        from_address = os.environ.get("SMTP_FROM", "").strip() or username

        use_ssl = _env_bool("SMTP_USE_SSL", default=(port == 465))
        use_starttls = _env_bool("SMTP_USE_STARTTLS", default=not use_ssl)

        return cls(
            host=host,
            port=port,
            username=username,
            password=password,
            from_address=from_address,
            from_name=os.environ.get("SMTP_FROM_NAME", "arXiv Buddy").strip(),
            use_ssl=use_ssl,
            use_starttls=use_starttls,
        )


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    """Everything a run needs: who to email, what they care about, how to send."""

    email: str
    interests: str
    model: str
    smtp: SmtpConfig | None
    candidate_pool_size: int
    state_path: Path | None
    api_base_url: str | None

    @classmethod
    def from_env(cls, *, require_smtp: bool = True) -> Config:
        email = _require("ARXIV_BUDDY_EMAIL")
        interests = _require("ARXIV_BUDDY_INTERESTS")

        raw_pool = os.environ.get("ARXIV_BUDDY_POOL_SIZE", "").strip()
        try:
            pool = int(raw_pool) if raw_pool else 60
        except ValueError as exc:
            raise ConfigError(
                f"ARXIV_BUDDY_POOL_SIZE must be a number, got {raw_pool!r}"
            ) from exc

        raw_state = os.environ.get("ARXIV_BUDDY_STATE", "").strip()

        return cls(
            email=email,
            interests=interests,
            model=os.environ.get("ARXIV_BUDDY_MODEL", "").strip() or DEFAULT_MODEL,
            smtp=SmtpConfig.from_env() if require_smtp else None,
            candidate_pool_size=max(10, min(pool, 200)),
            state_path=Path(raw_state).expanduser() if raw_state else None,
            api_base_url=os.environ.get("ANTHROPIC_BASE_URL", "").strip() or None,
        )
