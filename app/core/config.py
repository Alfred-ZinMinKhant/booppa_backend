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
    SUPPORT_EMAIL: str = "evidence@booppa.io"

    # Resend (preferred over SES — set RESEND_API_KEY to enable)
    RESEND_API_KEY: Optional[str] = None

    # ── Blockchain — Testnet (default, cost-free) ─────────────────────────
    # Today: Polygon Amoy Testnet. Gas = zero. Suitable for all customers.
    # Set USE_MAINNET=true in .env only after completing the mainnet migration
    # checklist (see docs/mainnet_migration.md).
    POLYGON_RPC_URL: str = "https://rpc-amoy.polygon.technology"
    POLYGON_EXPLORER_URL: str = "https://amoy.polygonscan.com"
    POLYGON_NETWORK_NAME: str = "Polygon Amoy Testnet"
    POLYGON_TESTNET_NOTICE: str = (
        "Anchored on Polygon Amoy Testnet — tamper-evident hash record. "
        "Note: Amoy is a public test network; it provides proof-of-existence "
        "suitable for audit readiness but does not carry the finality guarantees "
        "of Polygon Mainnet. Mainnet anchoring is available on request."
    )
    ANCHOR_CONTRACT_ADDRESS: str = "0x0000000000000000000000000000000000000000"
    PRIVATE_KEY_ENCRYPTED: Optional[str] = None
    BLOCKCHAIN_PRIVATE_KEY: Optional[str] = None

    # ── Blockchain — Mainnet (future, requires MATIC balance) ─────────────
    # Cost: ~0.001–0.01 MATIC per tx. Enable only after the mainnet migration
    # checklist and 20+ active Enterprise clients (see docs/mainnet_migration.md).
    USE_MAINNET: bool = False  # SAFE DEFAULT: False = testnet
    POLYGON_MAINNET_RPC_URL: str = "https://polygon-rpc.com"
    POLYGON_MAINNET_EXPLORER_URL: str = "https://polygonscan.com"
    POLYGON_MAINNET_CONTRACT_ADDRESS: Optional[str] = None
    POLYGON_MAINNET_NETWORK_NAME: str = "Polygon Mainnet"

    # CSP Compliance Pack
    CSP_MONTHLY_FEE_SGD: float = 299.0  # drives the liability cap (= 12 x monthly fee)

    # AI Services
    DEEPSEEK_API_KEY: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    # Anthropic / Claude
    ANTHROPIC_API_KEY: Optional[str] = None

    # Security intelligence (free tiers)
    # VirusTotal: free API key from https://www.virustotal.com/gui/join-us
    # Limits: 4 req/min, 500 req/day — sufficient for RFP generation use case
    VIRUSTOTAL_API_KEY: Optional[str] = None

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
    SKIP_PDF_GENERATION: bool = False
    SKIP_EMAIL: bool = False
    VERIFY_BASE_URL: str = "https://www.booppa.io"
    # Public base URL of THIS backend, used to build stable re-presign links
    # (e.g. the RFP DOCX download endpoint) that outlive S3 presigned expiry.
    # Set to the backend's public/tunnel origin; falls back to VERIFY_BASE_URL,
    # which proxies `/api` to the backend in the standard deployment.
    API_PUBLIC_BASE_URL: str = ""

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

    # Feature Flags
    FEATURE_COMPARISON: bool = False
    FEATURE_SEO: bool = False
    FEATURE_RANKING: bool = False
    FEATURE_GRAPH: bool = False
    FEATURE_COMPETITION: bool = False
    FEATURE_INSIGHT: bool = False
    FEATURE_PROCUREMENT_AUTOMATION: bool = False

    # Auto-activation check interval (seconds)
    AUTO_ACTIVATION_INTERVAL: int = 3600  # 1 hour

    # ── Computed blockchain properties (use these everywhere, not the raw fields) ──

    @property
    def active_polygon_rpc_url(self) -> str:
        """Return the correct RPC URL based on USE_MAINNET flag."""
        return self.POLYGON_MAINNET_RPC_URL if self.USE_MAINNET else self.POLYGON_RPC_URL

    @property
    def active_polygon_explorer_url(self) -> str:
        """Return the correct block explorer URL based on USE_MAINNET flag."""
        return self.POLYGON_MAINNET_EXPLORER_URL if self.USE_MAINNET else self.POLYGON_EXPLORER_URL

    @property
    def active_polygon_network_name(self) -> str:
        """Return the human-readable network name for use in PDFs and emails."""
        return self.POLYGON_MAINNET_NETWORK_NAME if self.USE_MAINNET else self.POLYGON_NETWORK_NAME

    @property
    def active_anchor_contract_address(self) -> str:
        """Return the correct contract address for the active network."""
        if self.USE_MAINNET:
            return self.POLYGON_MAINNET_CONTRACT_ADDRESS or self.ANCHOR_CONTRACT_ADDRESS
        return self.ANCHOR_CONTRACT_ADDRESS

    @property
    def blockchain_notice(self) -> str:
        """Return the appropriate disclosure notice for PDFs/emails."""
        if self.USE_MAINNET:
            return (
                "Anchored on Polygon Mainnet — permanent, tamper-evident record "
                "on a public blockchain. Verifiable independently at polygonscan.com."
            )
        return self.POLYGON_TESTNET_NOTICE

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "allow"


settings = Settings()

if settings.ENVIRONMENT == "production" and settings.SECRET_KEY == "change-me-in-production":
    raise RuntimeError(
        "FATAL: SECRET_KEY is still the default value. "
        "Set a strong SECRET_KEY environment variable before running in production."
    )

# ── Mainnet safety guard ──────────────────────────────────────────────────────
# Warn loudly in logs if mainnet is enabled without a proper contract address or
# signing key. Prevents silent fall-through to the null-address on Polygon Mainnet
# (which would burn MATIC anchoring to address 0x000…000).
if settings.USE_MAINNET:
    import logging as _logging
    _guard_log = _logging.getLogger(__name__)
    if not settings.POLYGON_MAINNET_CONTRACT_ADDRESS:
        _guard_log.warning(
            "USE_MAINNET=True but POLYGON_MAINNET_CONTRACT_ADDRESS is not set. "
            "Blockchain anchoring will target the null address. "
            "Set POLYGON_MAINNET_CONTRACT_ADDRESS in .env before enabling mainnet."
        )
    if not settings.BLOCKCHAIN_PRIVATE_KEY and not settings.PRIVATE_KEY_ENCRYPTED:
        _guard_log.warning(
            "USE_MAINNET=True but no BLOCKCHAIN_PRIVATE_KEY is configured. "
            "Transactions cannot be signed. Set BLOCKCHAIN_PRIVATE_KEY in .env."
        )
