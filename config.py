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

    def __init__(self, **kwargs):
        import os
        super().__init__(**kwargs)
        env_mode = os.getenv("MATCHER_MODE")
        if env_mode:
            self.matcher_mode = env_mode.lower().strip()

    # Server Settings
    host: str = "127.0.0.1"
    port: int = 8000

settings = Settings()
settings.matcher_audit_log_path.parent.mkdir(exist_ok=True)

