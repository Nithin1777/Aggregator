"""
Flyrobo (www.flyrobo.in) vendor adapter.

Flyrobo is an OpenCart/Journal storefront. HTML responses (product pages and
listings) are protected by a small JavaScript cookie challenge that sets an
``IoI`` cookie and reloads the page. The challenge value is a deterministic
FNV-1a hash of a token embedded in the response, so the adapter can solve it
without a browser: no Playwright dependency and no JavaScript execution.

Ingestion is sitemap-driven, matching the ground-truth approach used for Robu:

* ``sitemap.xml`` lists 10 ``sitemap-product-N.xml`` files (~5,000 products).
* Page 1 of ``fetch_catalog_page`` refreshes the registry in
  ``vendor_catalog_registry``; every later page is a stable slice of that
  registry, so the shared ``scrape_checkpoints`` continuation works unchanged.
* Product pages are parsed from schema.org JSON-LD (price, availability, SKU,
  model/id, image) with HTML/meta fallbacks.

Flyrobo prices are already tax-inclusive ("inc GST"), so no GST conversion is
applied.
"""
import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

from adapters.base import BaseAdapter, VendorFetchError
from config import settings
import db
from models import UnifiedProduct
from normalizer import extract_mpn, normalize_title

logger = logging.getLogger(__name__)

_CHALLENGE_TOKEN_RE = re.compile(r"H='([0-9a-fA-F]+)'")
_PRODUCT_SITEMAP_RE = re.compile(r"/sitemap-product-(\d+)\.xml$")
_PRODUCT_ID_RE = re.compile(r'name="product_id"\s+value="(\d+)"')
_QUICKVIEW_ID_RE = re.compile(r"quickview\('(\d+)'\)")
_GACLICK_ID_RE = re.compile(r"GAClick\([^)]*?,\s*'(\d+)'")
_GACLICK_CATEGORY_RE = re.compile(r",'([^']*> [^']*)','[^']*'\)")
_JSONLD_TYPE_PRODUCT = "product"


class FlyroboAdapter(BaseAdapter):
    """Adapter for Flyrobo.in (OpenCart + schema.org JSON-LD + sitemap registry)."""

    def __init__(self):
        self._vendor_id = "flyrobo"
        self._vendor_name = "Flyrobo"
        self._base_url = settings.flyrobo_base_url.rstrip("/")
        self._sitemap_url = settings.flyrobo_sitemap_url
        self._batch_limit = max(1, int(getattr(settings, "flyrobo_batch_limit", 20)))
        self._search_limit = min(
            100, max(1, int(getattr(settings, "flyrobo_search_limit", 100)))
        )
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

    # ------------------------------------------------------------------
    # HTTP session and anti-bot cookie challenge
    # ------------------------------------------------------------------

    async def _get_session(self) -> AsyncSession:
        async with self._lock:
            if self._session is None:
                self._session = AsyncSession(
                    impersonate=settings.impersonate_target,
                    headers={
                        "User-Agent": settings.user_agent,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                )
            return self._session

    async def _reset_session(self) -> None:
        """Drops cookies/session so a challenged or rate-limited request starts clean."""
        async with self._lock:
            if self._session is not None:
                try:
                    await self._session.close()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
                self._session = None

    @staticmethod
    def _is_cookie_challenge(text: str) -> bool:
        return bool(text) and "sessionStorage.IoI" in text

    @staticmethod
    def _solve_ioi(text: str) -> Optional[str]:
        """
        Returns the ``IoI`` cookie value for a challenge page.

        The page contains ``H='<hex>'`` and computes the cookie as the 32-bit
        FNV-1a hash of that string, rendered as lowercase hex without padding.
        """
        match = _CHALLENGE_TOKEN_RE.search(text or "")
        if not match:
            return None
        value = 2166136261
        for char in match.group(1):
            value ^= ord(char)
            value = (value * 16777619) & 0xFFFFFFFF
        return format(value, "x")

    async def _fetch_text(
        self,
        url: str,
        params: Optional[dict] = None,
        timeout: int = 30,
    ) -> Optional[str]:
        """
        GET a Flyrobo URL, solving the IoI challenge when present.

        Returns None only for a genuine 404. Network errors, rate limits and
        unparsable challenge pages raise VendorFetchError so ingestion never
        mistakes a failure for the end of the catalog.
        """
        session = await self._get_session()
        retries = 0
        challenge_attempts = 0
        last_error: Optional[str] = None

        while retries < settings.max_retries:
            try:
                await asyncio.sleep(max(0.0, float(settings.flyrobo_request_delay)))
                response = await session.get(url, params=params, timeout=timeout)
                status = response.status_code

                if status == 200:
                    text = response.text
                    if self._is_cookie_challenge(text):
                        token = self._solve_ioi(text)
                        if token is None or challenge_attempts >= 2:
                            raise VendorFetchError(
                                f"[{self.vendor_name}] Cookie challenge could not be solved "
                                f"for {url}."
                            )
                        host = urlsplit(url).hostname or urlsplit(self._base_url).hostname
                        session.cookies.set("IoI", token, domain=host)
                        challenge_attempts += 1
                        logger.debug("[%s] Solved IoI challenge for %s", self.vendor_name, url)
                        continue
                    return text

                if status == 404:
                    logger.info("[%s] 404 Not Found at %s.", self.vendor_name, url)
                    return None

                if status in (401, 403, 429) or 500 <= status < 600:
                    last_error = f"HTTP {status}"
                    delay = max(0.5, float(settings.backoff_factor) * (2 ** retries))
                    logger.warning(
                        "[%s] %s at %s. Backing off %.1fs (attempt %s/%s)...",
                        self.vendor_name, last_error, url, delay,
                        retries + 1, settings.max_retries,
                    )
                    if status in (401, 403, 429):
                        await self._reset_session()
                        session = await self._get_session()
                    await asyncio.sleep(delay)
                    retries += 1
                    continue

                last_error = f"HTTP {status}"
                logger.warning(
                    "[%s] Unexpected status %s at %s", self.vendor_name, status, url
                )
                retries += 1
                await asyncio.sleep(1.0)
            except VendorFetchError:
                raise
            except Exception as exc:  # noqa: BLE001 - classify and retry
                last_error = str(exc)
                logger.error(
                    "[%s] Network error requesting %s: %s", self.vendor_name, url, exc
                )
                retries += 1
                await asyncio.sleep(1.5)

        raise VendorFetchError(
            f"[{self.vendor_name}] Exceeded {settings.max_retries} attempts for {url} "
            f"({last_error or 'no usable response'}). This is a fetch failure, not end-of-catalog."
        )

    # ------------------------------------------------------------------
    # Sitemap registry
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_sitemap_index(xml: str) -> List[str]:
        """Returns product sitemap URLs in numeric order (ignores category/info maps)."""
        soup = BeautifulSoup(xml, "xml")
        urls = []
        for loc in soup.find_all("loc"):
            value = (loc.get_text() or "").strip()
            if _PRODUCT_SITEMAP_RE.search(value):
                urls.append(value)
        if not urls:
            # BeautifulSoup's XML parser is normally available (lxml); keep a
            # regex fallback so a parser change cannot silently disable ingestion.
            urls = [
                value.strip()
                for value in re.findall(
                    r"<loc>\s*(?:<!\[CDATA\[)?([^<\]]+)", xml
                )
                if _PRODUCT_SITEMAP_RE.search(value.strip())
            ]
        unique = sorted(set(urls), key=lambda u: int(_PRODUCT_SITEMAP_RE.search(u).group(1)))
        return unique

    @staticmethod
    def _parse_product_sitemap(xml: str) -> List[Tuple[str, str]]:
        """Extracts (slug, canonical URL) pairs from one product sitemap."""
        soup = BeautifulSoup(xml, "xml")
        entries: List[Tuple[str, str]] = []
        for node in soup.find_all("url"):
            loc = node.find("loc")
            if loc is None:
                continue
            url = (loc.get_text() or "").strip()
            if not url:
                continue
            slug = urlsplit(url).path.rstrip("/").split("/")[-1]
            if slug:
                entries.append((slug, url))

        if not entries:
            for url in re.findall(r"<loc>\s*(?:<!\[CDATA\[)?([^<\]]+)", xml):
                url = url.strip()
                slug = urlsplit(url).path.rstrip("/").split("/")[-1]
                if slug:
                    entries.append((slug, url))
        # De-duplicate while preserving sitemap order.
        return list(dict.fromkeys(entries))

    async def sync_sitemaps_to_registry(self) -> int:
        """
        Refreshes the ground-truth product registry from Flyrobo's sitemaps.

        A partially fetched sitemap set never marks products as missing: those
        rows stay eligible until a complete sync proves they were retired.
        """
        logger.info("[%s] Synchronizing product registry from sitemaps...", self.vendor_name)
        index_xml = await self._fetch_text(self._sitemap_url, timeout=45)
        if index_xml is None:
            raise VendorFetchError(
                f"[{self.vendor_name}] Sitemap index returned 404: {self._sitemap_url}"
            )

        sitemap_urls = self._parse_sitemap_index(index_xml)
        if not sitemap_urls:
            raise VendorFetchError(
                f"[{self.vendor_name}] Sitemap index contained no product sitemaps; "
                f"refusing to treat the catalog as empty."
            )

        entries: List[Tuple[str, str]] = []
        complete = True
        for sitemap_url in sitemap_urls:
            xml = await self._fetch_text(sitemap_url, timeout=45)
            if xml is None:
                complete = False
                logger.warning("[%s] Product sitemap 404: %s", self.vendor_name, sitemap_url)
                continue
            batch = self._parse_product_sitemap(xml)
            if not batch:
                # An empty 200 response is more likely a transient/markup problem
                # than a storefront deleting every product in that sitemap. Treat
                # the sync as partial so the retired-URL sweep cannot mass-delete.
                complete = False
                logger.warning(
                    "[%s] Product sitemap yielded no URLs: %s", self.vendor_name, sitemap_url
                )
            entries.extend(batch)

        if not entries:
            raise VendorFetchError(
                f"[{self.vendor_name}] No product URLs were parsed from "
                f"{len(sitemap_urls)} sitemap(s); refusing to mark the catalog complete."
            )

        # De-duplicate across sitemap files while keeping the first occurrence.
        entries = list(dict.fromkeys(entries))
        existing = db.list_vendor_registry_slugs(self._vendor_id)
        added = db.populate_vendor_registry(self._vendor_id, entries)

        if complete:
            live_slugs = {slug for slug, _ in entries}
            stale = sorted(existing - live_slugs)
            if stale:
                removed = db.mark_vendor_slugs_status(self._vendor_id, stale, "missing")
                logger.info(
                    "[%s] Marked %s retired sitemap URL(s) as missing.",
                    self.vendor_name, removed,
                )
        else:
            logger.warning(
                "[%s] Sitemap sync incomplete; skipped the retired-URL sweep.",
                self.vendor_name,
            )

        stats = db.get_vendor_registry_stats(self._vendor_id)
        logger.info(
            "[%s] Registry sync complete: %s sitemaps, %s URLs (%s new), %s indexed.",
            self.vendor_name,
            len(sitemap_urls),
            stats["total_registry"],
            added,
            stats["indexed"],
        )
        return added

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_price(value: Any) -> float:
        """Extracts the first INR amount from a string like '₹1,299.00'."""
        if value is None:
            return 0.0
        text = str(value).replace("\xa0", " ")
        match = re.search(r"(\d[\d,]*(?:\.\d+)?)", text)
        if not match:
            return 0.0
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            return 0.0

    @staticmethod
    def _clean_url(base: str, href: str) -> str:
        """Makes a product URL absolute and strips search/tracking query strings."""
        href = (href or "").strip()
        if not href:
            return ""
        parts = urlsplit(urljoin(base, href))
        path = parts.path or "/"
        return f"{parts.scheme}://{parts.netloc}{path}"

    @staticmethod
    def _clean_title(value: str) -> str:
        return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()

    @staticmethod
    def _jsonld_product(soup: BeautifulSoup) -> Optional[Dict[str, Any]]:
        """Returns the first schema.org Product node from a page's JSON-LD blocks."""
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                continue

            nodes: List[Any] = []
            if isinstance(payload, list):
                nodes = payload
            elif isinstance(payload, dict):
                if isinstance(payload.get("@graph"), list):
                    nodes = payload["@graph"]
                else:
                    nodes = [payload]

            for node in nodes:
                if not isinstance(node, dict):
                    continue
                raw_type = node.get("@type", "")
                types = raw_type if isinstance(raw_type, list) else [raw_type]
                if any(_JSONLD_TYPE_PRODUCT == str(t).lower() for t in types):
                    return node
        return None

    @staticmethod
    def _first_offer(node: Dict[str, Any]) -> Dict[str, Any]:
        offers = node.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        return offers if isinstance(offers, dict) else {}

    @staticmethod
    def _brand_name(node: Dict[str, Any]) -> Optional[str]:
        brand = node.get("brand")
        if isinstance(brand, list):
            brand = brand[0] if brand else None
        if isinstance(brand, dict):
            brand = brand.get("name")
        name = str(brand or "").strip()
        return name or None

    @staticmethod
    def _image_url(node: Dict[str, Any]) -> Optional[str]:
        image = node.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get("url")
        value = str(image or "").strip()
        if value.startswith("//"):
            value = f"https:{value}"
        return value or None

    @staticmethod
    def _category_from_breadcrumb(soup: BeautifulSoup) -> Optional[str]:
        crumbs = [
            FlyroboAdapter._clean_title(node.get_text())
            for node in soup.select("ul.breadcrumb li a")
        ]
        # The first crumb is the storefront logo link ("Home" or an empty label).
        crumbs = [
            crumb for crumb in crumbs
            if crumb and crumb.strip().lower() not in {"home", "home page"}
        ]
        return " > ".join(crumbs) if crumbs else None

    def _parse_product_card(self, card, query: str) -> Optional[UnifiedProduct]:
        """Parses one product card from a search/list page."""
        name_el = card.select_one(".name a") or card.select_one(".image a")
        if name_el is None:
            return None

        title = self._clean_title(name_el.get_text() or name_el.get("title", ""))
        if not title:
            img = card.select_one("img[alt]")
            title = self._clean_title(img.get("alt", "")) if img else ""
        if not title:
            return None

        href = name_el.get("href") or ""
        image_el = card.select_one(".image img") or card.select_one("img")
        if not href and image_el is not None:
            href = image_el.find_parent("a").get("href", "") if image_el.find_parent("a") else ""
        product_url = self._clean_url(self._base_url, href)
        if not product_url:
            return None

        onclick = " ".join(
            (el.get("onclick") or "") for el in card.select("[onclick]")
        )
        id_match = _QUICKVIEW_ID_RE.search(onclick) or _GACLICK_ID_RE.search(onclick)
        if id_match:
            external_id = id_match.group(1)
        else:
            hidden = card.select_one('input[name="product_id"]')
            external_id = hidden.get("value") if hidden is not None else ""
        external_id = str(external_id or urlsplit(product_url).path.rstrip("/").split("/")[-1])

        price = 0.0
        for selector in (".price-new", ".price-normal", ".price-old", ".price"):
            element = card.select_one(selector)
            if element is not None:
                price = self._parse_price(element.get_text(" ", strip=True))
                if price > 0:
                    break
        if price <= 0:
            return None

        old_el = card.select_one(".price-old")
        old_price = self._parse_price(old_el.get_text()) if old_el is not None else 0.0

        layout = card.find_parent(
            "div", class_=lambda classes: classes and "product-layout" in classes
        )
        layout_classes = layout.get("class", []) if layout is not None else []
        card_text = card.get_text(" ", strip=True)
        available = "out-of-stock" not in layout_classes and not re.search(
            r"out of stock|sold out", card_text, re.IGNORECASE
        )

        image_url = None
        if image_el is not None:
            src = image_el.get("data-src") or image_el.get("src")
            if src:
                image_url = urljoin(self._base_url, src)

        category = None
        category_match = _GACLICK_CATEGORY_RE.search(onclick)
        if category_match:
            category = self._clean_title(category_match.group(1))

        brand = None
        brand_el = card.select_one(".stats .stat-1 a")
        if brand_el is not None:
            brand = self._clean_title(brand_el.get_text())

        listed_sku = None
        sku_el = card.select_one(".stats .stat-2 span:last-child")
        if sku_el is not None:
            listed_sku = self._clean_title(sku_el.get_text())

        return UnifiedProduct(
            vendor=self.vendor_id,
            vendor_name=self.vendor_name,
            external_id=external_id,
            title=title,
            normalized_title=normalize_title(title),
            mpn=extract_mpn(title),
            sku=None,
            price=price,
            available=available,
            product_url=product_url,
            image_url=image_url,
            category=category,
            extra={
                "slug": urlsplit(product_url).path.rstrip("/").split("/")[-1],
                "product_id": external_id,
                "brand": brand,
                "listed_sku": listed_sku,
                "price_old": old_price or None,
                "price_includes_gst": True,
                "search_query": query,
                "source": "search",
            },
        )

    def _parse_search_page(self, html: str, query: str) -> List[UnifiedProduct]:
        soup = BeautifulSoup(html, "html.parser")
        products: List[UnifiedProduct] = []
        for card in soup.select("div.product-thumb"):
            try:
                product = self._parse_product_card(card, query)
                if product is not None:
                    products.append(product)
            except Exception as exc:  # noqa: BLE001 - one bad card must not kill a page
                logger.debug("[%s] Failed to parse search card: %s", self.vendor_name, exc)
        return products

    def _parse_product_page(
        self, html: str, url: str, slug: Optional[str] = None
    ) -> Optional[UnifiedProduct]:
        """Parses a product detail page, preferring schema.org JSON-LD."""
        soup = BeautifulSoup(html, "html.parser")
        node = self._jsonld_product(soup) or {}
        offer = self._first_offer(node)

        title = self._clean_title(str(node.get("name") or ""))
        if not title:
            title_el = soup.select_one("h1.page-title span") or soup.select_one("h1.page-title")
            title = self._clean_title(title_el.get_text() if title_el is not None else "")
        if not title:
            meta_title = soup.select_one('meta[property="og:title"]')
            title = self._clean_title(meta_title.get("content", "") if meta_title else "")
        if not title:
            return None

        product_url = self._clean_url(self._base_url, url)
        slug = slug or urlsplit(product_url).path.rstrip("/").split("/")[-1]

        external_id = str(node.get("model") or "").strip()
        if not external_id:
            id_match = _PRODUCT_ID_RE.search(html)
            external_id = id_match.group(1) if id_match else ""
        # Do not fall back to quickview IDs here: product pages embed related-item
        # carousels whose IDs would be attributed to the main product.
        external_id = external_id or slug

        price = self._parse_price(offer.get("price") if offer else None)
        if price <= 0:
            price_meta = soup.select_one('meta[property="product:price:amount"]')
            if price_meta is not None:
                price = self._parse_price(price_meta.get("content"))
        # Related-product carousels use the same price classes as the main product,
        # so DOM price fallbacks stay inside the product-info block.
        product_scope = soup.select_one(".product-info") or soup
        if price <= 0:
            for selector in (
                ".product-price-new",
                ".price-new",
                ".product-price-normal",
                ".price-normal",
                ".product-price-old",
            ):
                element = product_scope.select_one(selector)
                if element is not None:
                    price = self._parse_price(element.get_text(" ", strip=True))
                    if price > 0:
                        break
        if price <= 0:
            return None

        availability = str(offer.get("availability") or "").lower()
        if "outofstock" in availability or "out of stock" in availability:
            available = False
        elif "instock" in availability or "in stock" in availability:
            available = True
        else:
            available = True
            info = soup.select_one(".product-info")
            if info is not None and "out-of-stock" in info.get("class", []):
                available = False
            stock = soup.select_one(".product-stock")
            if stock is not None and "out-of-stock" in stock.get("class", []):
                available = False
            meta_availability = soup.select_one('meta[property="product:availability"]')
            if meta_availability is not None:
                meta_value = str(meta_availability.get("content") or "").lower()
                available = "in stock" in meta_value or "instock" in meta_value

        image_url = self._image_url(node)
        if not image_url:
            og_image = soup.select_one('meta[property="og:image"]')
            if og_image is not None:
                image_url = (og_image.get("content") or "").strip() or None

        sku = str(node.get("sku") or "").strip() or None
        if not sku:
            model_el = soup.select_one(".product-model span")
            if model_el is not None:
                candidate = self._clean_title(model_el.get_text())
                if candidate and candidate != external_id:
                    sku = candidate

        mpn = str(node.get("mpn") or "").strip() or extract_mpn(title)

        category = self._category_from_breadcrumb(soup)
        if not category:
            category_match = re.search(r'"item_category"\s*:\s*"([^"]+)"', html)
            if category_match:
                category = category_match.group(1)

        old_price = 0.0
        old_el = (
            product_scope.select_one(".product-price-old")
            or product_scope.select_one(".price-old")
        )
        if old_el is not None:
            old_price = self._parse_price(old_el.get_text(" ", strip=True))

        stock_note = None
        note_el = soup.select_one(".product-label-diagonal")
        if note_el is not None:
            stock_note = self._clean_title(note_el.get_text(" ", strip=True)) or None
        # The diagonal stock label is the storefront's own "Out Of Stock" badge;
        # trust it if the JSON-LD availability happened to be stale.
        if available and stock_note and re.search(
            r"out\s*of\s*stock|sold\s*out", stock_note, re.IGNORECASE
        ):
            available = False

        return UnifiedProduct(
            vendor=self.vendor_id,
            vendor_name=self.vendor_name,
            external_id=external_id,
            title=title,
            normalized_title=normalize_title(title),
            mpn=mpn,
            sku=sku,
            price=price,
            available=available,
            product_url=product_url,
            image_url=image_url,
            category=category,
            extra={
                "slug": slug,
                "product_id": external_id,
                "brand": self._brand_name(node),
                "price_old": old_price or None,
                "price_includes_gst": True,
                "stock_note": stock_note,
                "source": "sitemap",
            },
        )

    # ------------------------------------------------------------------
    # BaseAdapter API
    # ------------------------------------------------------------------

    async def search_live(self, query: str, limit: int = 20) -> List[UnifiedProduct]:
        """Live search via OpenCart's ``route=product/search`` listing."""
        clean_q = query.strip()
        if not clean_q:
            return []

        limit = max(1, int(limit))
        page_limit = min(limit, self._search_limit)
        search_url = f"{self._base_url}/index.php"
        products: List[UnifiedProduct] = []
        page = 1

        while len(products) < limit:
            html = await self._fetch_text(
                search_url,
                params={
                    "route": "product/search",
                    "search": clean_q,
                    "limit": page_limit,
                    "page": page,
                },
            )
            if html is None:
                break
            batch = self._parse_search_page(html, clean_q)
            if not batch:
                break
            products.extend(batch)
            if len(batch) < page_limit:
                break
            page += 1

        return products[:limit]

    async def fetch_catalog_page(
        self, page: int, limit: Optional[int] = None
    ) -> Tuple[List[UnifiedProduct], bool]:
        """
        Fetches one stable slice of the sitemap registry.

        Page 1 refreshes the registry from Flyrobo's sitemap index first, so a
        completed catalog can be re-scraped with ``ingest flyrobo --fresh``.
        """
        page = max(1, int(page))
        batch_size = max(1, int(limit or self._batch_limit))

        if page == 1:
            await self.sync_sitemaps_to_registry()

        offset = (page - 1) * batch_size
        # Enumerate *all* rows, including ones previously marked `missing`. The
        # shared checkpoint stores only a page number, so its offset is computed
        # as page * batch_size; excluding rows would shift every later offset and
        # silently skip products after a single 404.
        total = db.count_vendor_registry(self._vendor_id, include_missing=True)
        if offset >= total:
            logger.info(
                "[%s] Registry exhausted at offset %s of %s.", self.vendor_name, offset, total
            )
            return [], False

        rows = db.get_vendor_registry_page(
            self._vendor_id, offset, batch_size, include_missing=True
        )
        if not rows:
            return [], False

        products: List[UnifiedProduct] = []
        failed = 0
        missing = 0
        skipped_missing = 0

        for row in rows:
            slug = row["slug"]
            url = row["url"]

            if row.get("status") == "missing":
                # Already proven gone by an earlier run; keep the slot in the
                # enumeration so the registry offsets stay stable.
                missing += 1
                skipped_missing += 1
                continue

            try:
                html = await self._fetch_text(url)
            except VendorFetchError as exc:
                failed += 1
                db.mark_vendor_slug_status(self._vendor_id, slug, "failed")
                logger.warning("[%s] Product fetch failed for %s: %s", self.vendor_name, slug, exc)
                continue

            if html is None:
                missing += 1
                db.mark_vendor_slug_status(self._vendor_id, slug, "missing")
                logger.info("[%s] Product URL is gone (404): %s", self.vendor_name, url)
                continue

            product = self._parse_product_page(html, url, slug)
            if product is None:
                failed += 1
                db.mark_vendor_slug_status(self._vendor_id, slug, "failed")
                logger.warning(
                    "[%s] Product page parsed no usable product (price/markup): %s",
                    self.vendor_name, url,
                )
                continue

            products.append(product)
            db.mark_vendor_slug_status(self._vendor_id, slug, "indexed")

        attempted = len(rows) - skipped_missing
        if not products and failed and failed == attempted:
            raise VendorFetchError(
                f"[{self.vendor_name}] All {failed} product page(s) on registry slice "
                f"starting at offset {offset} failed; refusing to advance the checkpoint."
            )

        has_more = offset + len(rows) < total
        logger.info(
            "[%s] Page %s: %s product(s), %s failed, %s missing (offset %s/%s).",
            self.vendor_name, page, len(products), failed, missing, offset, total,
        )
        return products, has_more

    async def close(self):
        await self._reset_session()
