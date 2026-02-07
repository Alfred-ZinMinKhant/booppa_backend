from pydantic_settings import BaseSettings
from typing import Optional, List
import os


class Settings(BaseSettings):
    # Application
    SECRET_KEY: str = "change-me-in-production"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    ENVIRONMENT: str = "production"

    # CORS
    ALLOWED_ORIGINS: str = "http://localhost:3000"

    # Database
    DATABASE_URL: str = "postgresql+psycopg2://booppa:password@localhost:5432/booppa"
    REDIS_URL: str = "redis://localhost:6379/0"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 300

    # AWS
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None
    AWS_REGION: str = "ap-southeast-1"
    S3_BUCKET: str = "booppa-reports"

    # AWS SES
    AWS_SES_REGION: str = "ap-southeast-1"
    SUPPORT_EMAIL: str = "support@booppa.com"

    # Blockchain
    POLYGON_RPC_URL: str = "https://polygon-rpc.com"
    ANCHOR_CONTRACT_ADDRESS: str = "0x0000000000000000000000000000000000000000"
    PRIVATE_KEY_ENCRYPTED: Optional[str] = None
    BLOCKCHAIN_PRIVATE_KEY: Optional[str] = None

    # AI Services
    DEEPSEEK_API_KEY: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    # Anthropic / Claude
    ANTHROPIC_API_KEY: Optional[str] = None
    ANTHROPIC_MODEL: str = "claude-haiku-4.5"

    # Stripe
    STRIPE_SECRET_KEY: Optional[str] = None
    STRIPE_WEBHOOK_SECRET: Optional[str] = None

    # Monitoring
    GRAFANA_OTEL_ENDPOINT: Optional[str] = None
    PROMETHEUS_PORT: int = 9090

    # Booppa Monitor v5.5++
    MONITOR_CACHE_DIR: str = ".cache/booppa_monitor"
    MONITOR_CONCURRENCY_LIMIT: int = 100
    MONITOR_RISK_THRESHOLDS: dict[str, int] = {
        "LOW": 30,
        "MEDIUM": 60,
        "HIGH": 100,
    }

    # Report generation
    SKIP_PDF_GENERATION: bool = True
    VERIFY_BASE_URL: str = "https://verify.booppa.io"

    # Monitor v5.5++
    MONITOR_CACHE_DIR: str = ".cache/monitor"
    MONITOR_CONCURRENCY_LIMIT: int = 100
    MONITOR_RISK_THRESHOLDS: dict = {
        "LOW": 30,
        "MEDIUM": 60,
        "HIGH": 100,
    }
    MONITOR_SCAN1_COMMAND: Optional[str] = "python Scan1.py {url}"
    MONITOR_ANCHOR_ENABLED: bool = True

    # Rate Limiting
    RATE_LIMIT_REQUESTS: int = 100
    RATE_LIMIT_WINDOW: int = 60
    # Admin token for lightweight admin endpoints (set in environment)
    ADMIN_TOKEN: str | None = None
    # Optional basic auth for admin endpoints
    ADMIN_USER: str | None = None
    ADMIN_PASSWORD: str | None = None

    # Demo booking configuration
    BOOKING_TIMEZONE: str = "Asia/Bangkok"
    BOOKING_WORKING_DAYS: str = "0,1,2,3,4"  # Monday-Friday
    BOOKING_MORNING_SLOTS: str = "9,10,11"
    BOOKING_AFTERNOON_SLOTS: str = "14,15,16"
    BOOKING_MAX_PER_SLOT: int = 1
    BOOKING_DAYS_AHEAD: int = 60

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "allow"


settings = Settings()
