"""Configuration loaded from `.env` (never hard-code credentials)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    pass


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(value: str | None, default: int, name: str) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


@dataclass
class Config:
    base_url: str
    account_id: int
    api_token: str
    archive_inbox_id: int
    archive_inbox_name: str
    source_timezone: str
    data_dir: Path
    media_dir: Path
    output_dir: Path
    requests_per_minute: int
    conversation_messages_per_minute: int
    timestamp_prefix: bool
    http_timeout: int
    media_timeout: int

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.source_timezone)

    @property
    def state_db_path(self) -> Path:
        return self.output_dir / "migration_state.db"

    @property
    def log_path(self) -> Path:
        return self.output_dir / "migration.log"

    def require_chatwoot(self) -> None:
        """Raise a clear error when the Chatwoot settings are incomplete."""
        missing = []
        if not self.base_url:
            missing.append("CHATWOOT_BASE_URL")
        if not self.account_id:
            missing.append("CHATWOOT_ACCOUNT_ID")
        if not self.api_token:
            missing.append("CHATWOOT_API_TOKEN")
        if not self.archive_inbox_id:
            missing.append("CHATWOOT_ARCHIVE_INBOX_ID")
        if missing:
            raise ConfigError(
                "Missing required settings in .env: " + ", ".join(missing) + " (copy .env.example to .env and fill it in)"
            )


def load_config(env_file: Path | None = None, overrides: dict | None = None) -> Config:
    env_path = env_file or (PROJECT_DIR / ".env")
    if env_path.exists():
        load_dotenv(env_path, override=False)
    overrides = overrides or {}

    def get(name: str, default: str = "") -> str:
        if name in overrides and overrides[name] is not None:
            return str(overrides[name])
        return os.environ.get(name, default)

    def path(name: str, default: str) -> Path:
        raw = get(name, default)
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = PROJECT_DIR / p
        return p

    tz_name = get("SOURCE_TIMEZONE", "Asia/Dubai").strip() or "Asia/Dubai"
    try:
        ZoneInfo(tz_name)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"SOURCE_TIMEZONE {tz_name!r} is not a valid IANA timezone name") from exc

    return Config(
        base_url=get("CHATWOOT_BASE_URL", "").strip().rstrip("/"),
        account_id=_int(get("CHATWOOT_ACCOUNT_ID"), 0, "CHATWOOT_ACCOUNT_ID"),
        api_token=get("CHATWOOT_API_TOKEN", "").strip(),
        archive_inbox_id=_int(get("CHATWOOT_ARCHIVE_INBOX_ID"), 0, "CHATWOOT_ARCHIVE_INBOX_ID"),
        archive_inbox_name=get("CHATWOOT_ARCHIVE_INBOX_NAME", "Respond.io Archive").strip(),
        source_timezone=tz_name,
        data_dir=path("DATA_DIR", "data"),
        media_dir=path("MEDIA_DIR", "media"),
        output_dir=path("OUTPUT_DIR", "output"),
        requests_per_minute=_int(get("REQUESTS_PER_MINUTE"), 240, "REQUESTS_PER_MINUTE"),
        conversation_messages_per_minute=_int(
            get("CONVERSATION_MESSAGES_PER_MINUTE"), 150, "CONVERSATION_MESSAGES_PER_MINUTE"
        ),
        timestamp_prefix=_bool(get("TIMESTAMP_PREFIX"), False),
        http_timeout=_int(get("HTTP_TIMEOUT_SECONDS"), 60, "HTTP_TIMEOUT_SECONDS"),
        media_timeout=_int(get("MEDIA_TIMEOUT_SECONDS"), 120, "MEDIA_TIMEOUT_SECONDS"),
    )
