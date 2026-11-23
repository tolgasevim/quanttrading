"""Runtime configuration, read from environment variables (see .env.example)."""

import json
from decimal import Decimal
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
    # Email digests (FR-71): any SMTP server, such as a relay with a free tier. Empty host: no mail
    # is sent. The password is a secret: keep it in .env only.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = True
    smtp_from: str = "quant@localhost"
    # Telegram (FR-72): the token of a bot made with @BotFather, and the bot's name for the
    # instructions. Empty token: no Telegram.
    telegram_bot_token: str | None = None
    telegram_bot_name: str | None = None

    # Passkeys (WebAuthn, FR-2). The relying-party id is the host name people type in the browser
    # (no scheme, no port) and the origin is the full address of the web app. They must match the
    # real address, or the browser refuses the ceremony. The defaults suit a local trial only.
    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "QuantTrading"
    webauthn_origin: str = "http://localhost:3000"

    # Data providers, tried in order (PRD §8). Swapping providers is a config change.
    # A list setting takes either a comma list (yahoo,stooq) or JSON (["yahoo","stooq"]).
    price_providers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["yahoo", "stooq", "coingecko", "fred"]
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
    commentary_cron: str = "0 18 * * sun"  # the weekly AI commentary (FR-54)
    # Pushing alerts to Telegram and sending the email digests (FR-71, FR-72). Both run through
    # the day, so an alert held back by quiet hours goes out when they end.
    push_cron: str = "*/15 6-23 * * *"
    digest_cron: str = "5 7-21 * * *"
    mapping_cron: str = "0 22 * * mon-fri"  # before the price job, so new ISINs get prices at once
    fx_cron: str = "30 16 * * mon-fri"
    backfill_days: int = 400

    instruments_file: str = "seed/instruments.yaml"

    # AI layer (FR-50..FR-57). Without a key the AI pages answer "not set up". The key is never
    # logged or returned by the API. Prices are US dollars per million tokens (Opus 5.5 list).
    anthropic_api_key: str | None = None
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"
    llm_max_tokens: int = 16000
    llm_price_input: Decimal = Decimal("4")
    llm_price_output: Decimal = Decimal("20")
    llm_price_cache_read: Decimal = Decimal("0.20")
    llm_price_cache_write: Decimal = Decimal("5")
    # Server-side fallbacks let the API answer with another model when the first is overloaded
    # or unavailable. The answer says which model replied (the usage ledger keeps it too).
    llm_fallbacks: bool = True
    llm_monthly_cap_eur: Decimal = Decimal("2000")  # all users together (D36, FR-56)
    llm_user_monthly_cap_eur: Decimal = Decimal("500")  # one user, so one person cannot use it all

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
