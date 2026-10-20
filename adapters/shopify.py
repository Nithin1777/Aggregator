"""
Shopify vendor adapter for QuartzComponents and MakerBazar.
"""
import asyncio
import logging
from typing import List, Optional, Tuple
from curl_cffi.requests import AsyncSession

from adapters.base import BaseAdapter, VendorFetchError
from config import settings
from models import UnifiedProduct
from normalizer import extract_mpn, normalize_title

logger = logging.getLogger(__name__)

class ShopifyAdapter(BaseAdapter):
    """Adapter for Shopify-powered electronic stores (QuartzComponents, MakerBazar)."""

    def __init__(
        self,
        vendor_id: str,
        vendor_name: str,
        base_url: str,
        gst_rate: float = 0.0,
    ):
        self._vendor_id = vendor_id
        self._vendor_name = vendor_name
        self._base_url = base_url.rstrip("/")
        self._gst_rate = max(0.0, float(gst_rate or 0.0))
        self._session: Optional[AsyncSession] = None

    def _with_gst(self, base_price: float) -> float:
        """Convert an ex-GST catalogue price to the tax-inclusive price."""
        if base_price <= 0 or self._gst_rate <= 0:
            return base_price
        return round(base_price * (1.0 + self._gst_rate), 2)

    def _price_note(self, base_price: float) -> Optional[str]:
        if base_price <= 0 or self._gst_rate <= 0:
            return None
        return (
            f"₹{base_price:,.2f} + {self._gst_rate * 100:.0f}% GST "
            f"(tax-inclusive price shown)"
        )

    @property
    def vendor_id(self) -> str:
        return self._vendor_id

    @property
    def vendor_name(self) -> str:
        return self._vendor_name

    @property
    def base_url(self) -> str:
        return self._base_url

    async def _get_session(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(
                impersonate=settings.impersonate_target,
                headers={"User-Agent": settings.user_agent}
            )
        return self._session

    async def _fetch_with_backoff(self, url: str, params: Optional[dict] = None) -> Optional[dict]:
        """
        Performs GET request with exponential backoff on HTTP 429 rate limit.

        Returns None ONLY for a genuine 404 (end of catalog). Any other failure
        raises VendorFetchError so callers do not mistake it for an empty page.
        """
        session = await self._get_session()
        retries = 0
        last_error: Optional[str] = None

        while retries < settings.max_retries:
            try:
                response = await session.get(url, params=params, timeout=20)
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:
                    sleep_time = (2 ** retries) * settings.backoff_factor
                    logger.warning(
                        f"[{self.vendor_name}] Rate limited (429). Retrying in {sleep_time:.1f}s (attempt {retries + 1}/{settings.max_retries})"
                    )
                    await asyncio.sleep(sleep_time)
                    retries += 1
                    last_error = "HTTP 429 rate limited"
                elif response.status_code == 404:
                    logger.info(f"[{self.vendor_name}] Received 404 at {url}. End of catalog reached.")
                    return None
                else:
                    last_error = f"HTTP {response.status_code}"
                    logger.warning(f"[{self.vendor_name}] Unexpected status {response.status_code} at {url}")
                    retries += 1
                    await asyncio.sleep(1.0)
            except Exception as e:
                last_error = str(e)
                logger.error(f"[{self.vendor_name}] Network error requesting {url}: {e}")
                retries += 1
                await asyncio.sleep(1.0)

        raise VendorFetchError(
            f"[{self.vendor_name}] Exceeded {settings.max_retries} attempts for {url} ({last_error}). "
            f"This is a fetch failure, not end-of-catalog."
        )

    async def fetch_catalog_page(self, page: int, limit: int = 250) -> Tuple[List[UnifiedProduct], bool]:
        """
        Fetches one page of catalog products from /products.json.
        Returns: (List[UnifiedProduct], has_more)
        """
        url = f"{self._base_url}/products.json"
        params = {"limit": min(limit, settings.shopify_batch_limit), "page": page}

        # Rate-limiting polite delay between batch pages
        await asyncio.sleep(settings.shopify_page_delay)

        data = await self._fetch_with_backoff(url, params=params)
        if data is None:  # 404 only: genuine end of catalog
            return [], False
        if "products" not in data:
            # A 200 response without the expected envelope means the endpoint or its
            # schema changed. Treat as an error rather than as an empty last page.
            raise VendorFetchError(
                f"[{self.vendor_name}] Unexpected /products.json payload (no 'products' key) at page {page}."
            )

        raw_items = data.get("products", [])
        if not raw_items:
            return [], False

        products: List[UnifiedProduct] = []
        for item in raw_items:
            try:
                handle = item.get("handle", "")
                title = item.get("title", "").strip()
                if not title:
                    continue

                variants = item.get("variants", [])
                v0 = variants[0] if variants else {}

                price_raw = v0.get("price")
                try:
                    price_ex_gst = float(price_raw) if price_raw is not None else 0.0
                except (ValueError, TypeError):
                    price_ex_gst = 0.0
                price = self._with_gst(price_ex_gst)

                available = bool(v0.get("available", False))
                sku = v0.get("sku")
                external_id = str(item.get("id") or handle)

                # Images
                images = item.get("images", [])
                image_url = images[0].get("src") if images else None

                product_url = f"{self._base_url}/products/{handle}"
                norm_title = normalize_title(title)
                mpn = extract_mpn(title)

                category = item.get("product_type") or (item.get("tags", [None])[0] if item.get("tags") else None)

                prod = UnifiedProduct(
                    vendor=self.vendor_id,
                    vendor_name=self.vendor_name,
                    external_id=external_id,
                    title=title,
                    normalized_title=norm_title,
                    mpn=mpn,
                    sku=sku,
                    price=price,
                    available=available,
                    product_url=product_url,
                    image_url=image_url,
                    category=category,
                    price_note=self._price_note(price_ex_gst),
                    extra={
                        "handle": handle,
                        "variants_count": len(variants),
                        "tags": item.get("tags", []),
                        "price_ex_gst": price_ex_gst,
                        "gst_rate": self._gst_rate,
                    }
                )
                products.append(prod)
            except Exception as e:
                logger.debug(f"Error parsing Shopify item {item.get('id')}: {e}")
                continue

        # If count returned is less than requested limit, this is the last page
        has_more = len(raw_items) >= limit
        return products, has_more

    async def search_live(self, query: str, limit: int = 20) -> List[UnifiedProduct]:
        """
        Executes live search against Shopify's /search/suggest.json endpoint.
        """
        clean_q = query.strip()
        if not clean_q:
            return []

        url = f"{self._base_url}/search/suggest.json"
        params = {
            "q": clean_q,
            "resources[type]": "product",
            "resources[options][unavailable_products]": "show"
        }

        data = await self._fetch_with_backoff(url, params=params)
        if not data:
            return []

        raw_items = data.get("resources", {}).get("results", {}).get("products", [])
        products: List[UnifiedProduct] = []

        for item in raw_items[:limit]:
            try:
                title = item.get("title", "").strip()
                if not title:
                    continue

                price_raw = item.get("price")
                try:
                    price_ex_gst = float(price_raw) if price_raw is not None else 0.0
                except (ValueError, TypeError):
                    price_ex_gst = 0.0
                price = self._with_gst(price_ex_gst)

                available = bool(item.get("available", True))
                handle = item.get("handle") or ""
                item_url = item.get("url", f"/products/{handle}")
                
                # Make product URL absolute
                if item_url.startswith("http"):
                    product_url = item_url
                else:
                    product_url = f"{self._base_url}{item_url}"

                # Image URL
                image_url = item.get("image") or item.get("featured_image", {}).get("url")
                if image_url and image_url.startswith("//"):
                    image_url = f"https:{image_url}"

                external_id = str(item.get("id") or handle)
                norm_title = normalize_title(title)
                mpn = extract_mpn(title)

                prod = UnifiedProduct(
                    vendor=self.vendor_id,
                    vendor_name=self.vendor_name,
                    external_id=external_id,
                    title=title,
                    normalized_title=norm_title,
                    mpn=mpn,
                    sku=None,
                    price=price,
                    available=available,
                    product_url=product_url,
                    image_url=image_url,
                    category=item.get("type"),
                    price_note=self._price_note(price_ex_gst),
                    extra={
                        "handle": handle,
                        "price_ex_gst": price_ex_gst,
                        "gst_rate": self._gst_rate,
                    }
                )
                products.append(prod)
            except Exception as e:
                logger.debug(f"Error parsing Shopify suggest item: {e}")
                continue

        return products

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None
