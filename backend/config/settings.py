"""Application settings using Pydantic Settings for environment validation."""

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database Configuration
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data.db",
        description="Async database URL (SQLite for dev, PostgreSQL for prod)",
    )

    # Hyperliquid Trading Configuration
    hyperliquid_private_key: str = Field(
        ...,
        description="Hyperliquid private key for trading operations",
    )
    hyperliquid_wallet_address: str = Field(
        ...,
        description="Hyperliquid wallet address",
    )

    # DeepSeek AI Configuration
    deepseek_api_key: str = Field(
        ...,
        description="DeepSeek API key for AI trading decisions",
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com/v1",
        description="DeepSeek API base URL",
    )

    # Debug Settings
    debug: bool = Field(default=False, description="Enable debug mode")
    sql_debug: bool = Field(default=False, description="Enable SQL query logging")

    # Database Pool Configuration
    db_pool_size: int = Field(
        default=10,
        ge=3,
        le=15,
        description="Database connection pool size (3-15 range for single-user load)",
    )
    db_max_overflow: int = Field(
        default=5,
        ge=0,
        le=10,
        description="Maximum overflow connections beyond pool_size",
    )
    db_pool_timeout: int = Field(
        default=30,
        ge=10,
        le=60,
        description="Timeout in seconds for acquiring connection from pool",
    )

    # Sync Configuration
    sync_interval_seconds: int = Field(
        default=30,
        ge=10,
        le=300,
        description="Interval in seconds between Hyperliquid sync operations",
    )

    # AI Decision Configuration
    ai_decision_interval: int = Field(
        default=180,
        ge=60,
        le=600,
        description="Interval in seconds between AI trading decisions",
    )

    # Application Environment
    environment: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Application environment",
    )

    # CORS Configuration
    cors_origins: str = Field(
        default="*",
        description="Comma-separated list of allowed CORS origins, or * for all",
    )

    # Alerting Configuration (T137)
    alert_enabled: bool = Field(
        default=False,
        description="Enable alerting system",
    )
    alert_email_recipients: list[str] = Field(
        default_factory=list,
        description="List of email addresses to send alerts to",
    )
    alert_webhook_url: str = Field(
        default="",
        description="Webhook URL for alerts (Slack, Discord, or generic)",
    )

    # Learning System Configuration
    AUTO_APPLY_WEIGHTS: bool = Field(
        default=True,
        description="Enable automatic application of learned indicator weights once per day",
    )

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        """Validate database URL format."""
        if not v:
            raise ValueError("DATABASE_URL cannot be empty")
        if not (v.startswith("sqlite") or v.startswith("postgresql")):
            raise ValueError("DATABASE_URL must start with 'sqlite' or 'postgresql'")
        return v


# Global settings instance
settings = Settings()
