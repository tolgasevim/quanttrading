"""Runtime configuration, read from environment variables (see .env.example)."""

import json
from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import URL, make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QT_", env_file=".env", extra="ignore")

    # Either a full URL (QT_DATABASE_URL, must already be URL-encoded) or the parts below. The
    # parts are safer for generated passwords: characters such as @ : / # are escaped for you.
    database_url: str | None = None
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "quant"
    db_password: str = "quant"  # noqa: S105 - local-dev default; Compose passes the real one
    db_name: str = "quant"
    # Role the app switches to after connecting, so row-level security applies (FR-3).
    # Empty only for tooling that must act as the owner (migrations, test cleanup).
    db_app_role: str = "quant_app"
    max_upload_mb: int = 20

    # Auth
    session_ttl_hours: int = 24 * 14
    cookie_secure: bool = True
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    invite_ttl_hours: int = 72
    totp_issuer: str = "QuantTrading"

    # Data providers, tried in order (PRD §8). Swapping providers is a config change.
    # A list setting takes either a comma list (yahoo,stooq) or JSON (["yahoo","stooq"]).
    price_providers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["yahoo", "stooq", "coingecko"]
    )
    fx_provider: str = "ecb"
    # CoinGecko prices crypto in euros. It works without a key; a free demo key raises the limit.
    coingecko_api_key: str | None = None
    # ISIN -> ticker resolvers, tried in order (FR-12). OpenFIGI works without a key at a lower
    # rate limit; a free key (QT_OPENFIGI_API_KEY) raises it.
    isin_resolvers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["yahoo", "openfigi"]
    )
    openfigi_api_key: str | None = None
    isin_retry_days: int = 30  # how long to wait before asking again about an unknown ISIN
    http_timeout_seconds: float = 20.0
    http_retries: int = 3

    # Scheduler (Europe/Berlin): EOD prices after the US close, ECB FX after publication.
    timezone: str = "Europe/Berlin"
    prices_cron: str = "30 22 * * mon-fri"
    mapping_cron: str = "0 22 * * mon-fri"  # before the price job, so new ISINs get prices at once
    alerts_cron: str = "45 22 * * mon-fri"  # after the price job
    fx_cron: str = "30 16 * * mon-fri"
    backfill_days: int = 400

    instruments_file: str = "seed/instruments.yaml"

    @field_validator("price_providers", "isin_resolvers", mode="before")
    @classmethod
    def _list_from_env(cls, value: Any) -> Any:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return json.loads(text)
            return [part.strip() for part in text.split(",") if part.strip()]
        return value

    def db_url(self) -> URL:
        if self.database_url:
            return make_url(self.database_url)
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
