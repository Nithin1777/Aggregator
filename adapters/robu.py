"""
Robu.in vendor adapter using direct JSON search microservice with Cloudflare bypass
and ground-truth sitemap registry for 100% catalog coverage.
"""
import asyncio
import logging
import re
from typing import List, Optional, Tuple
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

from adapters.base import BaseAdapter, VendorFetchError
from config import settings
import db
from models import UnifiedProduct
from normalizer import extract_mpn, normalize_title

logger = logging.getLogger(__name__)

# Comprehensive high-frequency electronic taxonomy seeds covering component types,
# ICs, modules, connectors, and hardware across Robu's catalog.
ROBU_INGEST_SEEDS = [
    # Top component categories
    "module", "sensor", "board", "motor", "battery", "display", "wireless",
    "controller", "converter", "cable", "kit", "power", "switch", "relay",
    "connector", "camera", "drone", "robot", "arduino", "esp32", "stm32",
    "raspberry", "adapter", "driver", "encoder", "resistor", "capacitor",
    "diode", "transistor", "wire", "gear", "wheel", "servo", "stepper",
    "bldc", "lipo", "charger", "regulator", "buck", "boost", "tool",
    "soldering", "breadboard", "pcb", "terminal", "header", "antenna",
    "rfid", "bluetooth", "wifi", "lora", "gps", "oled", "lcd", "esc",
    "propeller", "frame", "fpv", "laser", "ultrasonic", "infrared",
    "current", "voltage", "gyro", "temperature", "humidity", "gas",
    "buzzer", "mosfet", "amplifier", "timer", "logic", "microcontroller",
    "potentiometer", "transformer", "fuse", "crystal", "oscillator",
    "inductor", "optocoupler", "led", "heatsink", "fan", "jack",
    "bearing", "pulley", "coupling", "solenoid", "pump", "valve",
    "multimeter", "tweezer", "tester", "programmer", "debugger",
    "ftdi", "uart", "spi", "i2c", "rs485", "can", "shield", "breakout",
    # Common high-volume trigrams in electronic components
    "mod", "sen", "boa", "mot", "bat", "wir", "pow", "res", "cap", "chi",
    "usb", "pin", "con", "pro"
]

class RobuAdapter(BaseAdapter):
    """Adapter for Robu.in headless Next.js search microservice with 100% catalog registry sync."""

    def __init__(self):
        self._vendor_id = "robu"
        self._vendor_name = "Robu.in"
        self._base_url = settings.robu_base_url
        self._session: Optional[AsyncSession] = None
        self._warmed_up = False
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

    async def _reset_session(self):
        """Discards existing session to clear challenged cookies and force fresh TLS handshake."""
        async with self._lock:
            if self._session:
                try:
                    await self._session.close()
                except Exception:
                    pass
                self._session = None
            self._warmed_up = False

    async def _get_session(self) -> AsyncSession:
        """Initializes AsyncSession with chrome120 fingerprint and performs Cloudflare warmup."""
        async with self._lock:
            if self._session is None:
                self._session = AsyncSession(
                    impersonate=settings.impersonate_target,
                    headers={"User-Agent": settings.user_agent}
                )
            if not self._warmed_up:
                try:
                    warmup_resp = await self._session.get(
                        f"{self._base_url}/",
                        headers={
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                            "Accept-Language": "en-US,en;q=0.9",
                        },
                        timeout=15
                    )
                    if warmup_resp.status_code == 200:
                        self._warmed_up = True
                        logger.debug("[Robu.in] Warmup successful (status: 200)")
                    elif warmup_resp.status_code in (403, 429):
                        logger.warning(f"[Robu.in] Cloudflare temporary IP rate limit active (status: {warmup_resp.status_code}).")
                except Exception as e:
                    logger.warning(f"[Robu.in] Warmup request error: {e}")
            return self._session

    async def sync_sitemaps_to_registry(self, max_sitemaps: int = 22) -> int:
        """
        Parses Robu's 22 XML sitemaps to populate the ground-truth product registry.
        Ensures 100% of published product URLs on Robu are accounted for.
        """
        session = await self._get_session()
        total_registered = 0

        logger.info(f"[Robu.in] Synchronizing ground-truth catalog registry from sitemaps 1..{max_sitemaps}...")
        for i in range(1, max_sitemaps + 1):
            url = f"{self._base_url}/products-sitemap/{i}/"
            for attempt in range(4):
                try:
                    resp = await session.get(url, headers={"Referer": f"{self._base_url}/sitemap.xml"}, timeout=30)
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.text, "xml")
                        locs = [loc.text.strip() for loc in soup.find_all("loc")]
                        if not locs:
                            break

                        slug_pairs = []
                        for loc in locs:
                            clean_loc = loc.rstrip("/")
                            slug = clean_loc.split("/")[-1]
                            if slug:
                                slug_pairs.append((slug, loc))

                        added = db.populate_robu_registry(slug_pairs)
                        total_registered += added
                        info_msg = f"+{added} new in registry" if added > 0 else "already recorded in registry"
                        logger.info(f"[Robu.in] Sitemap {i}/{max_sitemaps}: processed {len(slug_pairs)} URLs ({info_msg})")
                        break
                    elif resp.status_code in (403, 429):
                        sleep_time = (2 ** attempt) * 1.5
                        logger.warning(f"[Robu.in] Sitemap {i} received {resp.status_code}. Backing off {sleep_time:.1f}s (attempt {attempt + 1}/4)...")
                        self._warmed_up = False
                        await asyncio.sleep(sleep_time)
                    elif resp.status_code == 404:
                        break
                except Exception as e:
                    logger.warning(f"[Robu.in] Error fetching sitemap {i}: {e}. Retrying in 2.0s...")
                    await asyncio.sleep(2.0)

            await asyncio.sleep(settings.robu_request_delay)

        stats = db.get_robu_registry_stats()
        logger.info(f"[Robu.in] Catalog registry sync complete. Total ground-truth products: {stats['total_registry']:,}")
        return total_registered

    async def search_live(self, query: str, limit: int = 50, offset: int = 0) -> List[UnifiedProduct]:
        """
        Executes search against POST https://robu.in/api/search/
        """
        clean_q = query.strip()
        if not clean_q:
            return []

        session = await self._get_session()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": self._base_url,
            "Referer": f"{self._base_url}/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        payload = {
            "q": clean_q,
            "limit": min(limit, settings.robu_batch_limit),
            "offset": offset,
            "semantic": False,
            "sort": ""
        }

        retries = 0
        last_status = None
        last_error: Optional[str] = None
        while retries < settings.max_retries:
            try:
                await asyncio.sleep(settings.robu_request_delay)
                response = await session.post(
                    settings.robu_search_api,
                    headers=headers,
                    json=payload,
                    timeout=20
                )
                last_status = response.status_code
                if response.status_code == 200:
                    data = response.json()
                    products_data = data.get("data", {}).get("products", [])
                    return self._parse_robu_products(products_data)
                elif response.status_code in (403, 429):
                    sleep_time = (2 ** retries) * settings.backoff_factor
                    logger.warning(
                        f"[Robu.in] Cloudflare challenged (HTTP {response.status_code}). "
                        f"Purging session and backing off {sleep_time:.1f}s (attempt {retries + 1}/{settings.max_retries})..."
                    )
                    await self._reset_session()
                    await asyncio.sleep(sleep_time)
                    session = await self._get_session()
                    retries += 1
                else:
                    last_error = f"HTTP {response.status_code}"
                    logger.warning(f"[Robu.in] Search returned HTTP {response.status_code}")
                    retries += 1
                    await asyncio.sleep(settings.robu_request_delay)
            except Exception as e:
                last_error = str(e)
                logger.error(f"[Robu.in] Network error during search: {e}")
                retries += 1
                await asyncio.sleep(1.0)

        if last_status in (403, 429):
            raise ConnectionError(
                f"[Robu.in] Cloudflare temporary rate limit active (HTTP {last_status}) after {settings.max_retries} attempts."
            )

        # Exhausted retries without a usable response: this is a failure, not "no results".
        # Returning [] here would let catalog reconciliation mark slugs as indexed that
        # were never actually looked up.
        raise VendorFetchError(
            f"[Robu.in] Search for {clean_q!r} failed after {settings.max_retries} attempts "
            f"({last_error or 'no usable response'}). Treating as a fetch failure, not an empty result."
        )

    def _parse_robu_products(self, raw_items: list) -> List[UnifiedProduct]:
        """Converts Robu's typed JSON product items into UnifiedProduct models."""
        products: List[UnifiedProduct] = []
        for item in raw_items:
            try:
                name = item.get("name", "").strip()
                if not name:
                    continue

                item_id = str(item.get("id"))
                sku = item.get("sku")

                price_val = item.get("sale_price") or item.get("price") or 0.0
                try:
                    price = float(price_val)
                except (ValueError, TypeError):
                    price = 0.0

                available = bool(item.get("in_stock", False))

                slug = item.get("slug", "")
                product_url = f"https://robu.in/product/{slug}/" if slug else f"https://robu.in/?p={item_id}"
                image_url = item.get("image_url")

                norm_title = normalize_title(name)
                mpn = extract_mpn(name)

                cats = item.get("category_names", [])
                category = cats[0] if cats else None

                prod = UnifiedProduct(
                    vendor=self.vendor_id,
                    vendor_name=self.vendor_name,
                    external_id=item_id,
                    title=name,
                    normalized_title=norm_title,
                    mpn=mpn,
                    sku=sku,
                    price=price,
                    available=available,
                    product_url=product_url,
                    image_url=image_url,
                    category=category,
                    extra={
                        "slug": slug,
                        "sales_count": item.get("sales_count"),
                        "reviews_rating": item.get("reviews_rating"),
                        "categories": cats
                    }
                )
                products.append(prod)
            except Exception as e:
                logger.debug(f"[Robu.in] Error parsing product: {e}")
                continue

        return products

    async def fetch_catalog_page(self, page: int, limit: int = 50) -> Tuple[List[UnifiedProduct], bool]:
        """
        Continuable catalog ingestion for Robu.
        Stage 1: Paginates across electronic taxonomy seeds (fetching up to 250 products per seed).
        Stage 2: Reconciles remaining un-indexed slugs from the ground-truth sitemap registry to reach 100%.
        """
        # Auto-sync registry on first ingestion page if registry has not been initialized
        reg_stats = db.get_robu_registry_stats()
        if reg_stats["total_registry"] == 0:
            logger.info("[Robu.in] Ground-truth registry empty. Synchronizing sitemaps before ingestion...")
            await self.sync_sitemaps_to_registry()

        total_seeds = len(ROBU_INGEST_SEEDS)

        if page <= total_seeds:
            seed = ROBU_INGEST_SEEDS[page - 1]
            logger.info(f"[Robu.in] Ingesting page {page}/{total_seeds}: seed '{seed}'")
            products: List[UnifiedProduct] = []
            offset = 0
            batch_limit = 50
            max_per_seed = 250

            while offset < max_per_seed:
                batch = await self.search_live(query=seed, limit=batch_limit, offset=offset)
                if not batch:
                    break
                products.extend(batch)
                if len(batch) < batch_limit:
                    break
                offset += batch_limit
                await asyncio.sleep(settings.robu_request_delay)

            return products, True
        else:
            # Stage 2: Registry-targeted reconciliation for remaining unindexed items
            pending = db.get_pending_robu_slugs(limit=10)
            if not pending:
                # Sync any newly indexed products with registry
                db.sync_robu_registry_with_products()
                pending = db.get_pending_robu_slugs(limit=10)
                if not pending:
                    logger.info("[Robu.in] 100% of ground-truth catalog registry indexed!")
                    return [], False

            all_targeted_products: List[UnifiedProduct] = []
            for item in pending:
                slug = item["slug"]
                tokens = [t for t in re.split(r"[-_]", slug) if len(t) >= 3 and not t.isdigit()]
                search_term = " ".join(tokens[:2]) if len(tokens) >= 2 else (tokens[0] if tokens else slug)
                targeted = await self.search_live(query=search_term, limit=10)
                if targeted:
                    all_targeted_products.extend(targeted)
                db.mark_robu_slug_indexed(slug)
                await asyncio.sleep(settings.robu_request_delay)

            return all_targeted_products, True

    async def close(self):
        async with self._lock:
            if self._session:
                await self._session.close()
                self._session = None
                self._warmed_up = False
