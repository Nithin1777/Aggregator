"""
Data models and schemas for the Indian Component Aggregator Engine.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

class UnifiedProduct(BaseModel):
    """Normalized component model across all vendors."""
    id: Optional[int] = None
    vendor: str = Field(..., description="Unique vendor slug: robu, quartzcomponents, makerbazar")
    vendor_name: str = Field(..., description="Human-friendly vendor name")
    external_id: str = Field(..., description="Vendor internal ID or product handle")
    title: str = Field(..., description="Original product title from vendor")
    normalized_title: str = Field(..., description="Standardized cleaned title")
    mpn: Optional[str] = Field(None, description="Extracted Manufacturer Part Number / Chip ID")
    sku: Optional[str] = Field(None, description="Vendor product SKU")
    price: float = Field(default=0.0, description="Current selling price in INR")
    available: bool = Field(default=False, description="True if in stock and available for order")
    product_url: str = Field(..., description="Canonical product detail page URL")
    image_url: Optional[str] = Field(None, description="Direct URL to product thumbnail or image")
    category: Optional[str] = Field(None, description="Raw vendor category string")
    category_canonical: Optional[str] = Field(
        None,
        description="Canonical category slug (see config/taxonomy.yaml), e.g. 'resistors'"
    )
    is_canonical: bool = Field(default=False, description="True if marked as canonical product for a brand, category, or MPN")
    canonical_for: Optional[str] = Field(None, description="The canonical key this product represents")
    query_ctr: Optional[float] = Field(None, description="Click-through rate for query relevance")
    updated_at: Optional[str] = None
    price_updated_at: Optional[str] = Field(None, description="UTC timestamp of the last successful live price/stock fetch")
    price_source: Optional[str] = Field(None, description="'live' if price/stock was fetched from the vendor, 'cache' if never refreshed")
    extra: Dict[str, Any] = Field(default_factory=dict, description="Vendor-specific metadata")

class VendorOffer(BaseModel):
    """An offer from a specific vendor within a comparison group."""
    vendor: str
    vendor_name: str
    external_id: str
    title: str
    price: float
    available: bool
    product_url: str
    image_url: Optional[str] = None
    sku: Optional[str] = None
    category: Optional[str] = Field(None, description="Raw vendor category string")
    category_canonical: Optional[str] = Field(
        None, description="Canonical category slug (see config/taxonomy.yaml)"
    )
    merge_reason: Optional[str] = Field(None, description="Rule name or reason that merged this offer")
    variant_key: Optional[str] = Field(None, description="Unique variant identifier based on spec signature")
    source: Optional[str] = Field(default="v2_fallback", description="Source of merge decision: llm_label or v2_fallback")
    price_updated_at: Optional[str] = Field(None, description="UTC timestamp of the last live price/stock fetch for this offer")
    price_source: Optional[str] = Field(None, description="'live' if fetched from the vendor, 'cache' if served from the local catalog")

class ComparisonGroup(BaseModel):
    """Side-by-side comparison for identical or matching components across vendors."""
    canonical_name: str
    mpn: Optional[str] = None
    best_price: Optional[float] = None
    best_vendor: Optional[str] = None
    in_stock_count: int = 0
    total_offers: int = 0
    offers: List[VendorOffer] = Field(default_factory=list)
    category: Optional[str] = Field(None, description="Raw vendor category string")
    category_canonical: Optional[str] = Field(
        None, description="Canonical category slug (see config/taxonomy.yaml)"
    )
    variant_key: Optional[str] = Field(None, description="Unique variant spec key for this group")
    merge_reason: Optional[str] = Field(None, description="Merge reason rule name")
    variants: List[Dict[str, Any]] = Field(default_factory=list, description="Sibling variants within same family")
    distinct_titles: List[str] = Field(default_factory=list, description="Unique offer titles across vendors")
    source: str = Field(default="v2_fallback", description="Source of merge decision: llm_label or v2_fallback")


class PriceStockRefreshInfo(BaseModel):
    """
    Outcome of a search-time price/stock refresh.

    status:
      - 'live'             : price/stock were refreshed from vendors
      - 'partial'          : refreshed, but some vendors failed or timed out
      - 'cached'           : live refresh was attempted but unavailable; cached values served
      - 'fresh_cache'      : cached values were recent enough, no vendor call needed
      - 'offline_cooldown' : vendors recently unreachable; served cache without retrying
      - 'timeout'          : refresh bridge exceeded its wall-clock budget; cached values served
      - 'error'            : unexpected refresh failure; cached values served
      - 'disabled'         : live refresh turned off via configuration
    """
    status: str = "cached"
    attempted: bool = False
    candidates: int = 0
    refreshed_products: int = 0
    cached_products: int = 0
    vendors_ok: List[str] = Field(default_factory=list)
    vendors_failed: List[str] = Field(default_factory=list)
    queries: List[str] = Field(default_factory=list)
    fetched_at: Optional[str] = None
    message: str = ""


class SearchResponse(BaseModel):
    """Unified search response covering raw results and side-by-side comparison groups."""
    query: str
    total_results: int
    source: str = "live"  # 'live' or 'local_db'
    results: List[UnifiedProduct] = Field(default_factory=list)
    comparison_groups: List[ComparisonGroup] = Field(default_factory=list)
    price_refresh: Optional[PriceStockRefreshInfo] = Field(
        None, description="Outcome of the search-time price/stock refresh (live vs cached fallback)"
    )


class ScrapeState(BaseModel):
    """Persistence model tracking continuable ingestion progress per vendor."""
    vendor: str
    last_page: int = 0
    last_offset: int = 0
    total_ingested: int = 0
    status: str = "idle"  # 'idle', 'in_progress', 'completed', 'paused', 'error'
    last_error: Optional[str] = None
    updated_at: Optional[str] = None

class IngestProgress(BaseModel):
    """Real-time progress reporting during catalog ingestion."""
    vendor: str
    current_page: int
    pages_processed: int
    items_added: int
    total_vendor_items: int
    is_complete: bool = False
    message: str = ""

class ClickFeedback(BaseModel):
    """Click telemetry payload."""
    query: str
    group_id: str
    vendor: str
    product_id: str
