"""Runtime configuration, read from environment variables (see .env.example)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QT_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://quant:quant@localhost:5432/quant"

    # Auth
    session_ttl_hours: int = 24 * 14
    cookie_secure: bool = True
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    invite_ttl_hours: int = 72
    totp_issuer: str = "QuantTrading"

    # Data providers, tried in order (PRD §8). Swapping providers is a config change.
    price_providers: list[str] = Field(default_factory=lambda: ["yahoo", "stooq"])
    fx_provider: str = "ecb"
    http_timeout_seconds: float = 20.0
    http_retries: int = 3

    # Scheduler (Europe/Berlin): EOD prices after the US close, ECB FX after publication.
    timezone: str = "Europe/Berlin"
    prices_cron: str = "30 22 * * mon-fri"
    fx_cron: str = "30 16 * * mon-fri"
    backfill_days: int = 400

    instruments_file: str = "seed/instruments.yaml"


@lru_cache
def get_settings() -> Settings:
    return Settings()
