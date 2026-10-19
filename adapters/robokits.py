"""
Robokits India vendor adapter for Zen Cart legacy e-commerce engine.
Parses server-rendered HTML for catalog pagination and live component search.
"""
import asyncio
import logging
import re
from typing import List, Optional, Tuple
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

from adapters.base import BaseAdapter, VendorFetchError
from config import settings
from models import UnifiedProduct
from normalizer import extract_mpn, normalize_title

logger = logging.getLogger(__name__)

class RobokitsAdapter(BaseAdapter):
    """Adapter for Robokits India (Zen Cart PHP/MySQL backend)."""

    def __init__(self):
        self._vendor_id = "robokits"
        self._vendor_name = "Robokits India"
        self._base_url = settings.robokits_base_url.rstrip("/")
        self._session: Optional[AsyncSession] = None
        self._lock = asyncio.Lock()

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
        async with self._lock:
            if self._session is None:
                self._session = AsyncSession(
                    impersonate=settings.impersonate_target,
                    headers={
                        "User-Agent": settings.user_agent,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                    }
                )
            return self._session

    async def _fetch_html(self, url: str) -> Optional[str]:
        """
        Fetches page HTML with backoff on 500/502/503/429.

        Returns None ONLY for a genuine 404 (end of catalog). Any other failure
        raises VendorFetchError so it is never mistaken for an empty page.
        """
        session = await self._get_session()
        retries = 0
        last_error: Optional[str] = None

        while retries < settings.max_retries:
            try:
                response = await session.get(url, timeout=15)
                if response.status_code == 200:
                    return response.text
                elif response.status_code in (500, 502, 503, 429):
                    last_error = f"HTTP {response.status_code}"
                    logger.warning(
                        f"[{self.vendor_name}] Server error {response.status_code} at {url}. "
                        f"Backing off 3.0s (attempt {retries + 1}/{settings.max_retries})..."
                    )
                    await asyncio.sleep(3.0)
                    retries += 1
                elif response.status_code == 404:
                    logger.info(f"[{self.vendor_name}] 404 Not Found at {url}. End of catalog.")
                    return None
                else:
                    last_error = f"HTTP {response.status_code}"
                    logger.warning(f"[{self.vendor_name}] Unexpected status {response.status_code} for {url}")
                    retries += 1
                    await asyncio.sleep(1.0)
            except Exception as e:
                last_error = str(e)
                logger.error(f"[{self.vendor_name}] Network error requesting {url}: {e}")
                retries += 1
                await asyncio.sleep(2.0)

        raise VendorFetchError(
            f"[{self.vendor_name}] Exceeded {settings.max_retries} attempts for {url} ({last_error}). "
            f"This is a fetch failure, not end-of-catalog."
        )

    @staticmethod
    def _extract_price_from_text(txt: str) -> Optional[float]:
        if not txt:
            return None
        clean = re.sub(r"[^\d,\.]", " ", txt).strip()
        matches = re.findall(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", clean)
        for m in matches:
            try:
                val = float(m.replace(",", ""))
                if val > 0:
                    return val
            except ValueError:
                pass
        return None

    def _extract_card_price(self, card) -> float:
        """
        Extracts the correct purchase price from a Robokits product card.
        Prioritizes the sale/special price when discounted, then regular/base price.
        Never extracts the discount percentage badge (-12%).
        """
        # 1. Target explicit sale / special price elements
        sale_el = card.find(
            class_=lambda c: c and any(
                x in c.lower() for x in ("productspecialprice", "special", "price-new", "final")
            )
        )
        if sale_el:
            p = self._extract_price_from_text(sale_el.get_text())
            if p is not None:
                return p

        # 2. Target regular / base price elements (when no sale element exists)
        base_el = card.find(
            class_=lambda c: c and any(
                x in c.lower() for x in ("productbaseprice", "baseprice", "normalprice", "price-box__old", "price-old", "regular")
            )
        )
        if base_el:
            p = self._extract_price_from_text(base_el.get_text())
            if p is not None:
                return p

        # 3. Fallback: inspect any price-box container while ignoring discount badges
        pbox = card.find(class_=lambda c: c and "price" in c)
        if pbox:
            box_copy = BeautifulSoup(str(pbox), "html.parser")
            for badge in box_copy.find_all(
                class_=lambda c: c and any(x in c.lower() for x in ("discount", "label--sale", "badge", "label"))
            ):
                badge.decompose()
            for el in box_copy.find_all(string=lambda t: t and "%" in t):
                if el.parent:
                    el.parent.decompose()
            old_el = box_copy.find(class_=lambda c: c and any(x in c.lower() for x in ("old", "normalprice")))
            if old_el:
                old_el.decompose()
                p = self._extract_price_from_text(box_copy.get_text())
                if p is not None:
                    return p
            p = self._extract_price_from_text(box_copy.get_text())
            if p is not None:
                return p

        return 0.0

    def _parse_product_cards(self, html: str) -> List[UnifiedProduct]:
        """Extracts UnifiedProduct models from Zen Cart pzen-item product cards."""
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.find_all("div", class_="pzen-item")
        if not cards:
            # Fallback to general product container if pzen-item not present
            cards = soup.find_all("div", class_=lambda c: c and "product" in c and "product--zoom" in c)

        products: List[UnifiedProduct] = []
        for card in cards:
            try:
                # 1. Product Title & URL
                title_el = card.find("a", title=True)
                if not title_el:
                    name_container = card.find("div", class_="product__inside__name")
                    if name_container:
                        title_el = name_container.find("a")

                if not title_el:
                    continue

                title = title_el.get("title") or title_el.get_text(strip=True)
                title = re.sub(r"\s+", " ", title).strip()
                if not title:
                    continue

                raw_url = title_el.get("href", "")
                product_url = urljoin(self._base_url, raw_url)

                # 2. External ID (Zen Cart products_id)
                external_id = None
                action_btn = card.find("a", href=lambda h: h and "products_id=" in h)
                if action_btn:
                    id_match = re.search(r"products_id=(\d+)", action_btn["href"])
                    if id_match:
                        external_id = id_match.group(1)

                if not external_id:
                    id_match = re.search(r"products_id=(\d+)", product_url)
                    if id_match:
                        external_id = id_match.group(1)

                if not external_id:
                    # Fallback to URL path slug
                    clean_slug = product_url.rstrip("/").split("/")[-1]
                    external_id = clean_slug if clean_slug else str(abs(hash(title)))

                # 3. Model / SKU Extraction
                card_text = card.get_text(separator=" ", strip=True)
                model_match = re.search(r"(?:\[|Model:\s*)(RKI-\d+|RMCS-\d+)(?:\])?", card_text, re.IGNORECASE)
                sku = model_match.group(1) if model_match else None

                # Canonical MPN and Title Normalization
                norm_title = normalize_title(title)
                mpn = extract_mpn(title) or (sku if sku else None)

                # 4. Price Parsing (SALE price if discounted, else regular price; never discount %)
                price = self._extract_card_price(card)

                # 5. Stock Status
                is_out_of_stock = bool(re.search(r"out of stock|ships within|sold out", card_text, re.IGNORECASE))
                has_buy_now = bool(card.find("a", href=lambda h: h and "action=buy_now" in h))
                has_in_stock_text = bool(re.search(r"in stock|buy now|add to cart", card_text, re.IGNORECASE))

                available = (has_buy_now or has_in_stock_text) and not is_out_of_stock

                # 6. Image URL
                img_el = card.find("img")
                image_url = None
                if img_el:
                    src = img_el.get("src") or img_el.get("data-src")
                    if src:
                        image_url = urljoin(self._base_url, src)

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
                    category=None,
                    extra={
                        "model": sku,
                        "products_id": external_id
                    }
                )
                products.append(prod)
            except Exception as e:
                logger.debug(f"[{self.vendor_name}] Error parsing product card: {e}")
                continue

        return products

    async def search_live(self, query: str, limit: int = 20) -> List[UnifiedProduct]:
        """
        Executes live search against Robokits Zen Cart advanced search endpoint.
        """
        clean_q = query.strip()
        if not clean_q:
            return []

        search_url = (
            f"{self._base_url}/index.php?main_page=advanced_search_result"
            f"&search_in_description=1&keyword={quote_plus(clean_q)}"
        )
        html = await self._fetch_html(search_url)
        if not html:
            return []

        products = self._parse_product_cards(html)
        return products[:limit]

    async def fetch_catalog_page(self, page: int, limit: int = 30) -> Tuple[List[UnifiedProduct], bool]:
        """
        Fetches one catalog page from https://robokits.co.in/all?page={page}.
        Returns (products, has_more).
        """
        await asyncio.sleep(settings.robokits_page_delay)
        url = f"{self._base_url}/all?page={page}"
        logger.info(f"[{self.vendor_name}] Ingesting catalog page {page}: {url}")

        html = await self._fetch_html(url)
        if html is None:  # 404 only: genuine end of catalog
            return [], False

        # Termination checks
        if "Displaying 0 to 0" in html or "Displaying 0 to" in html:
            logger.info(f"[{self.vendor_name}] Reached end of catalog at page {page} ('Displaying 0 to 0').")
            return [], False

        products = self._parse_product_cards(html)
        if not products:
            # No explicit end-of-catalog marker yet zero cards parsed: this is almost
            # always a markup change, and treating it as "completed" would silently
            # truncate the catalog at that page.
            raise VendorFetchError(
                f"[{self.vendor_name}] Page {page} returned HTML but zero product cards were parsed "
                f"and no end-of-catalog marker was present. Possible markup change; refusing to "
                f"mark the catalog complete."
            )

        has_more = len(products) >= limit
        return products, has_more

    async def close(self):
        async with self._lock:
            if self._session:
                await self._session.close()
                self._session = None
