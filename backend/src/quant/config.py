"""Runtime configuration, read from environment variables (see .env.example)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
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
    price_providers: list[str] = Field(default_factory=lambda: ["yahoo", "stooq"])
    fx_provider: str = "ecb"
    # ISIN -> ticker resolvers, tried in order (FR-12). OpenFIGI works without a key at a lower
    # rate limit; a free key (QT_OPENFIGI_API_KEY) raises it.
    isin_resolvers: list[str] = Field(default_factory=lambda: ["yahoo", "openfigi"])
    openfigi_api_key: str | None = None
    isin_retry_days: int = 30  # how long to wait before asking again about an unknown ISIN
    http_timeout_seconds: float = 20.0
    http_retries: int = 3

    # Scheduler (Europe/Berlin): EOD prices after the US close, ECB FX after publication.
    timezone: str = "Europe/Berlin"
    prices_cron: str = "30 22 * * mon-fri"
    mapping_cron: str = "0 22 * * mon-fri"  # before the price job, so new ISINs get prices at once
    fx_cron: str = "30 16 * * mon-fri"
    backfill_days: int = 400

    instruments_file: str = "seed/instruments.yaml"

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
