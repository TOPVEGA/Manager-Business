
# الترخيص: @TopVeGa
# العلامة التجارية: VEGA & Logic
# حقوق الطبع والنشر: © 2015 - 2025


from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    bot_token: str = Field(
        default="توكن البوت",
        alias="BOT_TOKEN",
    )
    owner_id: int = Field(default=481322119, alias="OWNER_ID")
    bot_name: str = Field(default="TURKEY BUSINESS", alias="BOT_NAME")
    bot_channel_url: str = Field(
        default="https://t.me/vegaone", alias="BOT_CHANNEL_URL"
    )
    bot_channel_name: str = Field(
        default="قناة البوت", alias="BOT_CHANNEL_NAME"
    )
    developer_channel_url: str = Field(
        default="https://t.me/vegaone", alias="DEVELOPER_CHANNEL_URL"
    )
    developer_channel_name: str = Field(
        default="قناة المطور", alias="DEVELOPER_CHANNEL_NAME"
    )
    developer_url: str = Field(
        default="https://t.me/Topvega", alias="DEVELOPER_URL"
    )
    Dev: str = Field(
        default="𓏺 فيجا .", alias="Dev"
    )

    gift_asset_api_key: str = Field(
        default="tCTF_CT6ztJyxnfaWbfWJIkJaGGym3RACga8b_lm_Rg", 
        alias="GIFT_ASSET_API_KEY"
    )

    database_url: str = Field(
        default="sqlite:///manager.db",
        alias="DATABASE_URL",
    )
    db_pool_min: int = Field(2, alias="DB_POOL_MIN", ge=1, le=50)
    db_pool_max: int = Field(10, alias="DB_POOL_MAX", ge=1, le=100)
    db_command_timeout: int = Field(15, alias="DB_COMMAND_TIMEOUT", ge=5, le=120)

    max_concurrent_updates: int = Field(
        100, alias="MAX_CONCURRENT_UPDATES", ge=10
    )
    rate_limit_per_user: int = Field(30, alias="RATE_LIMIT_PER_USER", ge=1)
    rate_limit_window: int = Field(60, alias="RATE_LIMIT_WINDOW", ge=1)
    cache_ttl: int = Field(900, alias="CACHE_TTL", ge=10)
    settings_cache_ttl: int = Field(
        60, alias="SETTINGS_CACHE_TTL", ge=10, le=600
    )
    redis_url: str = Field("", alias="REDIS_URL")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    maintenance_mode: bool = Field(False, alias="MAINTENANCE_MODE")
    maintenance_message: str = Field(
        "<b>🛠 البوت تحت الصيانة حاليًا</b>\n\n"
        "سيتم استئناف الخدمة قريبًا. شكرًا لصبركم.",
        alias="MAINTENANCE_MSG",
    )

    use_webhook: bool = Field(False, alias="USE_WEBHOOK")
    webhook_url: str = Field("", alias="WEBHOOK_URL")
    webhook_secret: str = Field("", alias="WEBHOOK_SECRET")
    webhook_path: str = Field("/webhook", alias="WEBHOOK_PATH")
    webapp_host: str = Field("0.0.0.0", alias="WEBAPP_HOST")
    webapp_port: int = Field(8080, alias="WEBAPP_PORT", ge=1, le=65535)

    referral_enabled: bool = Field(False, alias="REFERRAL_ENABLED")
    referral_reward_days: int = Field(
        2, alias="REFERRAL_REWARD_DAYS", ge=1, le=365
    )
    referral_target: int = Field(
        3, alias="REFERRAL_TARGET", ge=1, le=100
    )

    @field_validator("owner_id")
    @classmethod
    def validate_owner_id(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("OWNER_ID must be a positive integer")
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {allowed}")
        return upper

    @field_validator("db_pool_max")
    @classmethod
    def validate_pool_max(cls, v: int, info) -> int:
        pool_min = info.data.get("db_pool_min", 1)
        if v < pool_min:
            raise ValueError("DB_POOL_MAX must be >= DB_POOL_MIN")
        return v

    @field_validator("webhook_path")
    @classmethod
    def validate_webhook_path(cls, v: str) -> str:
        if not v:
            return "/webhook"
        if not v.startswith("/"):
            v = "/" + v
        return v

    @model_validator(mode="after")
    def validate_webhook_config(self) -> "Settings":
        if self.use_webhook:
            if not self.webhook_url:
                raise ValueError(
                    "WEBHOOK_URL is required when USE_WEBHOOK=True"
                )
            if not self.webhook_secret:
                raise ValueError(
                    "WEBHOOK_SECRET is required when USE_WEBHOOK=True"
                )
            if len(self.webhook_secret) < 16:
                raise ValueError(
                    "WEBHOOK_SECRET must be at least 16 characters"
                )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
