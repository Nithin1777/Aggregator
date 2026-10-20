"""
Configuration and settings for the Indian Component Aggregator Engine.
"""
from pathlib import Path
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

class Settings(BaseModel):
    # Database
    db_path: Path = DATA_DIR / "components.db"

    # HTTP & Impersonation
    impersonate_target: str = "chrome120"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    # Vendor URLs
    quartz_base_url: str = "https://quartzcomponents.com"
    makerbazar_base_url: str = "https://makerbazar.in"
    robu_base_url: str = "https://robu.in"
    robu_search_api: str = "https://robu.in/api/search/"
    robokits_base_url: str = "https://robokits.co.in"
    rees52_base_url: str = "https://rees52.com"

    # Rate Limiting & Delays (seconds)
    shopify_page_delay: float = 0.4
    robu_request_delay: float = 1.2
    robokits_page_delay: float = 0.6
    max_retries: int = 5
    backoff_factor: float = 1.5

    # Batch Sizes
    shopify_batch_limit: int = 250
    robu_batch_limit: int = 50
    robokits_batch_limit: int = 30

    # Matching & Classification
    matcher_v2_enabled: bool = True
    matcher_mode: str = "hybrid"  # 'hybrid' or 'v2_only'
    matcher_audit_logging: bool = True
    matcher_audit_log_path: Path = BASE_DIR / "logs" / "matcher_audit.jsonl"

    # Live Price/Stock Refresh (search-time refresh with cached fallback)
    price_stock_refresh_enabled: bool = True
    # Skip live refresh when the cached price/stock was verified more recently than this.
    price_stock_ttl_seconds: int = 180
    # Hard wall-clock budget for one refresh pass across all vendors (parallel).
    price_stock_refresh_timeout_seconds: float = 8.0
    # Max distinct vendor search queries per refresh pass (user query + derived MPN/title fallbacks).
    price_stock_max_refresh_queries: int = 2
    # After a pass where every vendor failed, serve cache without retrying for this long.
    price_stock_offline_cooldown_seconds: float = 60.0
    # Per-vendor result limit used for the refresh lookups.
    price_stock_live_limit_per_vendor: int = 15

    def __init__(self, **kwargs):
        import os
        super().__init__(**kwargs)
        env_mode = os.getenv("MATCHER_MODE")
        if env_mode:
            self.matcher_mode = env_mode.lower().strip()

        env_refresh = os.getenv("PRICE_STOCK_REFRESH")
        if env_refresh is not None:
            self.price_stock_refresh_enabled = env_refresh.strip().lower() not in (
                "0", "false", "off", "no"
            )
        env_ttl = os.getenv("PRICE_STOCK_TTL")
        if env_ttl:
            try:
                self.price_stock_ttl_seconds = int(float(env_ttl))
            except ValueError:
                pass
        env_timeout = os.getenv("PRICE_STOCK_TIMEOUT")
        if env_timeout:
            try:
                self.price_stock_refresh_timeout_seconds = float(env_timeout)
            except ValueError:
                pass

    # Server Settings
    host: str = "127.0.0.1"
    port: int = 8000

settings = Settings()
settings.matcher_audit_log_path.parent.mkdir(exist_ok=True)

