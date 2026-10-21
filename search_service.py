"""
Aggregator search service: multi-vendor live federation, local FTS, side-by-side comparison clustering,
and continuable catalog ingestion.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
import sqlite3
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from urllib.parse import urlsplit
from adapters.base import BaseAdapter
from adapters.robu import RobuAdapter
from adapters.robokits import RobokitsAdapter
from adapters.shopify import ShopifyAdapter
from config import settings
import db
from models import (
    ComparisonGroup,
    IngestProgress,
    PriceStockRefreshInfo,
    SearchResponse,
    UnifiedProduct,
    VendorOffer,
)
from normalizer import (
    calculate_token_similarity,
    can_merge,
    canonical_base_mpn,
    check_spec_discriminator_conflict,
    classify_product_form,
    ComponentItem,
    extract_mpn,
    extract_spec_signature,
    get_canonical_cluster_key,
    get_canonical_cluster_key_v2,
    match_components_v2,
    normalize_title,
)
from src.search.retrieval import search_catalog_ranked, retrieve_catalog
from src.search.product_prior import coverage, query_affinity, query_category_hint
from src.search.query_intent import get_query_intent
from src.search.listing_identity import listing_role_conflict
from gray_zone import evaluate_gray_zone_risk, record_gray_zone_entry



logger = logging.getLogger(__name__)

# Clustering is O(n^2) in the worst case, but the normalizer's pure functions are
# memoized, so a 200-item page clusters in ~150 ms. The hard ceiling stays: 400
# items is ~80k pair comparisons, and anything larger blocks for seconds.
GROUPING_WARN_ITEMS = 400
MAX_GROUPING_ITEMS = 400

class AggregatorEngine:
    """Core aggregation and comparison engine for Indian electronic component vendors."""

    def __init__(self):
        # Initialize vendor adapters
        self.adapters: Dict[str, BaseAdapter] = {
            "robu": RobuAdapter(),
            "quartzcomponents": ShopifyAdapter(
                vendor_id="quartzcomponents",
                vendor_name="Quartz Components",
                base_url=settings.quartz_base_url,
                gst_rate=settings.quartz_gst_rate,
            ),
            "makerbazar": ShopifyAdapter(
                vendor_id="makerbazar",
                vendor_name="MakerBazar",
                base_url=settings.makerbazar_base_url
            ),
            "rees52": ShopifyAdapter(
                vendor_id="rees52",
                vendor_name="REES52",
                base_url=settings.rees52_base_url
            ),
            "robokits": RobokitsAdapter(),
        }
        # Ensure database tables exist
        db.init_db()
        self._llm_labels: Optional[Dict[str, Dict[str, Any]]] = None
        # Audit lines are queued and flushed once per grouping pass. Writing per pair
        # meant ~1200 open/write/close cycles per search (~550ms) and an unbounded log.
        self._pending_decisions: List[str] = []
        # Search-time price/stock refresh coordination. The plain flag avoids
        # duplicate vendor traffic when two page views hit at once; the cooldown is
        # a circuit breaker so an offline catalog serves cache instantly.
        self._refresh_in_progress = False
        self._refresh_cooldown_until = 0.0
        # Background (non-blocking) web refresh tasks, kept referenced until done.
        self._refresh_tasks: set = set()
        # Tracks which event loop owns the adapters' HTTP sessions. A reused engine
        # (tests, embedded servers) must not send sessions across loop boundaries.
        self._active_loop: Optional[asyncio.AbstractEventLoop] = None

    async def close(self):
        """Cleanly closes all adapter sessions."""
        for adapter in self.adapters.values():
            await adapter.close()

    def _load_llm_labels(self) -> Dict[str, Dict[str, Any]]:
        labels = {}
        try:
            conn = sqlite3.connect(str(settings.db_path), timeout=5.0)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='llm_labels'")
            if c.fetchone():
                c.execute("SELECT pair_id, label, confidence, reason FROM llm_labels")
                for r in c.fetchall():
                    labels[r["pair_id"]] = dict(r)
            conn.close()
        except Exception as e:
            logger.debug(f"Failed to load llm_labels: {e}")
        return labels

    def _append_decision_log(self, log_msg: str):
        """Writes a single audit line immediately (kept for callers outside the hot path)."""
        try:
            log_file = settings.matcher_audit_log_path.parent / "matcher_decisions.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now(timezone.utc).isoformat()} {log_msg}\n")
        except Exception:
            pass

    def _record_decision(self, log_msg: str) -> None:
        """Queues an audit line to be flushed once per grouping pass."""
        if not getattr(settings, "matcher_audit_logging", False):
            return
        self._pending_decisions.append(log_msg)

    def _flush_decisions(self) -> None:
        """Writes queued audit lines with a single file handle."""
        if not self._pending_decisions:
            return
        try:
            log_file = settings.matcher_audit_log_path.parent / "matcher_decisions.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).isoformat()
            with open(log_file, "a", encoding="utf-8") as f:
                f.write("".join(f"{stamp} {msg}\n" for msg in self._pending_decisions))
        except Exception:
            pass
        finally:
            self._pending_decisions.clear()

    def _evaluate_pair_merge(self, ci_a: ComponentItem, ci_b: ComponentItem) -> Tuple[bool, str, str]:
        """
        Evaluates pair merge decision with strict precedence:
        1. If hybrid mode and pair exists in llm_labels -> use label (same->merge, different/unsure->reject)
        2. Else fall back to can_merge (V2 matcher)
        Emits log: pair=<pair_id> source=<llm_label|v2_fallback> label=<label|->
        """
        conflict = listing_role_conflict(ci_a.title, ci_b.title)
        if conflict:
            return False, conflict, "identity_guard"
        norm = sorted([ci_a.title.lower().strip(), ci_b.title.lower().strip()])
        pair_id = hashlib.sha1("::".join(norm).encode("utf-8")).hexdigest()[:16]

        matcher_mode = getattr(settings, "matcher_mode", "hybrid").lower()
        if matcher_mode == "hybrid":
            if self._llm_labels is None:
                self._llm_labels = self._load_llm_labels()

            label_entry = self._llm_labels.get(pair_id)
            if label_entry:
                lbl = label_entry["label"].lower().strip()
                log_msg = f"pair={pair_id} source=llm_label label={lbl}"
                logger.debug(log_msg)
                self._record_decision(log_msg)
                if lbl == "same":
                    return True, f"llm_label:{label_entry.get('reason') or 'verified_same'}", "llm_label"
                elif lbl == "different":
                    return False, "llm_label:different", "llm_label"
                else:  # unsure -> leave unmerged (conservative)
                    return False, "llm_label:unsure", "llm_label"

        # Fallback to match_components_v2 via can_merge
        merged, reason = can_merge(ci_a, ci_b)

        # STEP 1: Universal spec guards on Tier A MPN matches
        if merged and ("tier a" in reason.lower() or "mpn match" in reason.lower()):
            has_conflict, conflict_reason = check_spec_discriminator_conflict(ci_a.title, ci_b.title)
            if has_conflict:
                log_msg = f"pair={pair_id} source=v2_fallback label=- veto={conflict_reason}"
                logger.debug(log_msg)
                self._record_decision(log_msg)
                return False, f"tier_a_spec_conflict:{conflict_reason}", "v2_fallback"

        # Only merges and vetoes are audited; rejections are the overwhelming majority
        # (~1200/search) and go to debug logging instead of the audit file.
        if merged:
            log_msg = f"pair={pair_id} source=v2_fallback label=- decision=merge"
            logger.debug(log_msg)
            self._record_decision(log_msg)
        else:
            logger.debug("pair=%s source=v2_fallback label=- decision=reject", pair_id)
        return merged, reason, "v2_fallback"


    def _log_matcher_decision(
        self,
        p1: UnifiedProduct,
        p2: UnifiedProduct,
        score: float,
        is_match: bool,
        reason: str,
        query: Optional[str] = None
    ):
        """Evaluates gray-zone risk flags and persists to JSONL and SQLite."""
        if not getattr(settings, "matcher_audit_logging", False):
            return
        try:
            entry = evaluate_gray_zone_risk(p1, p2, is_match, score, reason, query=query)
            if entry:
                record_gray_zone_entry(entry)
        except Exception as e:
            logger.debug(f"Gray zone logging failed: {e}")

    def group_and_compare(self, products: List[UnifiedProduct], query: Optional[str] = None) -> List[ComparisonGroup]:
        """
        Clusters products using Hybrid LLM-Labels + V2 Fallback:
        - Consults llm_labels by pair_id first; if present (same -> merge, different/unsure -> reject).
        - If not in llm_labels, falls back to can_merge (match_components_v2).
        - Assigns card/offer source as 'llm_label' or 'v2_fallback'.
        - Best price badge computed across same-spec offers only.

        Raises ValueError when given more than MAX_GROUPING_ITEMS products: the
        comparison is quadratic, so a large batch would block for minutes rather
        than return a result. Paginate at the call site instead.
        """
        if len(products) > MAX_GROUPING_ITEMS:
            pairs = len(products) * (len(products) - 1) // 2
            raise ValueError(
                f"group_and_compare received {len(products)} products (~{pairs:,} pair comparisons), "
                f"above the hard limit of {MAX_GROUPING_ITEMS}. Clustering is O(n^2) and this batch "
                f"would block for minutes. Pass a smaller page (the UI uses <=200, MCP uses <=50)."
            )
        if len(products) > GROUPING_WARN_ITEMS:
            logger.warning(
                "group_and_compare clustering %d products (~%d pair comparisons); expect slow results.",
                len(products), len(products) * (len(products) - 1) // 2
            )

        # 1. Enrich products with ComponentItem
        tagged_items: List[Tuple[UnifiedProduct, ComponentItem]] = []
        for p in products:
            ci = ComponentItem(p.title, p.price, mpn=p.mpn)
            tagged_items.append((p, ci))

        # 2. Strict grouping by evaluate_pair_merge
        clusters: List[Dict[str, Any]] = []
        cluster_orders: List[int] = []

        for order, (p, ci) in enumerate(tagged_items):
            placed = False
            for cluster in clusters:
                rep_ci = cluster["ci"]
                merged, reason, source = self._evaluate_pair_merge(ci, rep_ci)
                if merged:
                    cluster["items"].append((p, ci))
                    cluster["reasons"].append(reason)
                    cluster["sources"].append(source)
                    placed = True
                    break
            if not placed:
                clusters.append({"ci": ci, "items": [(p, ci)], "reasons": [], "sources": []})
                # Position of the best-ranked listing in this cluster. `products`
                # arrives in retrieval order, so this is the group's relevance.
                cluster_orders.append(order)

        # Flush queued audit lines once for the whole pass (single file open).
        self._flush_decisions()

        # 3. Build ComparisonGroup models
        result: List[ComparisonGroup] = []

        for cluster in clusters:
            rep_ci = cluster["ci"]
            items_in_cluster = [item[0] for item in cluster["items"]]

            # Pick shortest clean title as canonical
            canonical_name = min(items_in_cluster, key=lambda x: len(x.title)).title
            canonical_mpn = rep_ci.mpn or next((p.mpn for p in items_in_cluster if p.mpn), None)
            spec_sig = rep_ci.spec_sig

            if canonical_mpn and spec_sig:
                variant_key = f"{canonical_mpn}:{spec_sig}"
            else:
                p_first = items_in_cluster[0]
                variant_key = f"item:{p_first.vendor}_{p_first.external_id}"

            is_merged = len(items_in_cluster) > 1
            merge_reason = cluster["reasons"][0] if (is_merged and cluster["reasons"]) else "single_offer"
            card_source = "llm_label" if "llm_label" in cluster.get("sources", []) else "v2_fallback"

            # Build vendor offers
            offers_by_vendor: Dict[str, VendorOffer] = {}
            for p in items_in_cluster:
                offer = VendorOffer(
                    vendor=p.vendor,
                    vendor_name=p.vendor_name,
                    external_id=p.external_id,
                    title=p.title,
                    price=p.price,
                    available=p.available,
                    product_url=p.product_url,
                    image_url=p.image_url,
                    sku=p.sku,
                    category=p.category,
                    category_canonical=p.category_canonical,
                    merge_reason=merge_reason,
                    variant_key=variant_key,
                    source=card_source,
                    price_updated_at=p.price_updated_at,
                    price_source=p.price_source or "cache",
                    price_note=p.price_note,
                )
                if p.vendor not in offers_by_vendor:
                    offers_by_vendor[p.vendor] = offer
                else:
                    existing = offers_by_vendor[p.vendor]
                    if (not existing.available and offer.available) or (
                        offer.available == existing.available and offer.price < existing.price
                    ):
                        offers_by_vendor[p.vendor] = offer

            all_offers = list(offers_by_vendor.values())
            all_offers.sort(key=lambda o: (not o.available, o.price))

            # Best Price on same-spec offers only!
            in_stock_offers = [o for o in all_offers if o.available and o.price > 0]
            if in_stock_offers:
                best_offer = min(in_stock_offers, key=lambda o: o.price)
                best_price = best_offer.price
                best_vendor = best_offer.vendor_name
            elif all_offers:
                best_offer = min(all_offers, key=lambda o: o.price if o.price > 0 else 999999)
                best_price = best_offer.price
                best_vendor = best_offer.vendor_name
            else:
                best_price = None
                best_vendor = None

            group = ComparisonGroup(
                canonical_name=canonical_name,
                mpn=canonical_mpn,
                best_price=best_price,
                best_vendor=best_vendor,
                in_stock_count=len(in_stock_offers),
                total_offers=len(all_offers),
                offers=all_offers,
                category=next((p.category for p in items_in_cluster if p.category), None),
                category_canonical=next(
                    (p.category_canonical for p in items_in_cluster if p.category_canonical), None
                ),
                variant_key=variant_key,
                merge_reason=merge_reason,
                variants=[],
                source=card_source
            )
            result.append(group)

        # 4. Connect Variant Families (Same MPN + differing spec_sig -> sub-list under the card)
        family_map: Dict[str, List[ComparisonGroup]] = {}
        for g in result:
            if g.mpn:
                family_map.setdefault(g.mpn, []).append(g)

        for mpn, fam_groups in family_map.items():
            if len(fam_groups) > 1:
                for g in fam_groups:
                    g.variants = [
                        {
                            "variant_key": sib.variant_key,
                            "label": sib.variant_key.split(":")[-1] if ":" in (sib.variant_key or "") else sib.canonical_name,
                            "title": sib.canonical_name,
                            "from_price": sib.best_price
                        }
                        for sib in fam_groups
                        if sib.variant_key != g.variant_key
                    ]

        # Sort groups. With a text query, relevance wins: a group inherits the rank
        # position of its best-matching listing, gets a bonus when every query token
        # appears in its canonical name, and a smaller bonus for multi-vendor depth
        # (that is the whole point of a comparison engine). Without a query the page
        # is a category browse, so richer/better-priced groups come first.
        if query:
            category_hint = query_category_hint(query)
            exact_query = get_query_intent(query).intent == "exact_lookup"
            # Broad catalogue queries benefit more from vendor depth than an
            # exact MPN lookup, where the part itself must stay on top.
            offer_weight = 3.0 if exact_query else 4.0
            kit_query = bool(re.search(r"\b(kit|combo|set|pack|bundle|starter)\b", query, re.IGNORECASE))
            # Stock is a ranking signal, not only a tie-breaker: a card with no
            # purchasable offer should not sit above an in-stock alternative.
            in_stock_adjust = -2.0 if exact_query else -4.0
            out_of_stock_adjust = 1.0 if exact_query else 2.0
            scored: List[Tuple[float, int, int, str, ComparisonGroup]] = []
            for order, group in zip(cluster_orders, result):
                exact_bonus = (
                    8.0 if coverage(query, group.canonical_name) >= 1.0 else 0.0
                )
                # Cheap phrase match: "raspberry pi pico" as a contiguous run.
                phrase_bonus = 4.0 if query.strip().lower() in group.canonical_name.lower() else 0.0
                # Role/attachment prior: a card that *is* the part beats a card
                # that merely mentions it ("bracket for IG52" vs "IG52 motor").
                role = query_affinity(group.canonical_name, query)
                category_bonus = (
                    6.0
                    if category_hint and group.category_canonical == category_hint
                    else 0.0
                )
                stock_adjust = (
                    in_stock_adjust if group.in_stock_count > 0 else out_of_stock_adjust
                )
                # A bundle is a valid result, but for a bare component query it
                # should not sit above the individual part just because its title
                # is short. Explicit kit/combo searches skip this penalty.
                bundle_penalty = (
                    14.0
                    if not kit_query and classify_product_form(group.canonical_name) == "kit"
                    else 0.0
                )
                relevance = (
                    order
                    - offer_weight * min(group.total_offers, 3)
                    - exact_bonus
                    - phrase_bonus
                    - 4.0 * role
                    - category_bonus
                    + stock_adjust
                    + bundle_penalty
                )
                scored.append(
                    (relevance, -group.in_stock_count, -group.total_offers, group.canonical_name.lower(), group)
                )
            scored.sort(key=lambda item: item[:4])
            result = [item[4] for item in scored]
        else:
            result.sort(key=lambda g: (-g.total_offers, -g.in_stock_count, g.canonical_name))
        return result

    # ------------------------------------------------------------------
    # Search-time price/stock refresh (live values with cached fallback)
    # ------------------------------------------------------------------

    async def _ensure_adapter_loop(self) -> None:
        """Rebuild vendor sessions if the engine is reused on a different event loop."""
        loop = asyncio.get_running_loop()
        if self._active_loop is not None and self._active_loop is not loop:
            for adapter in self.adapters.values():
                try:
                    await adapter.close()
                except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                    logger.debug("Adapter session cleanup on loop change failed: %s", exc)
        self._active_loop = loop

    @staticmethod
    def _offer_url_key(url: Optional[str]) -> str:
        """Normalizes a product URL so vendor-side variants of one page compare equal."""
        if not url:
            return ""
        try:
            parts = urlsplit(url.strip().lower())
            return f"{parts.netloc}{parts.path.rstrip('/')}"
        except Exception:
            return url.strip().lower().rstrip("/")

    @staticmethod
    def _derive_refresh_query(product: UnifiedProduct) -> Optional[str]:
        """Builds a fallback vendor query from an MPN, else the leading title tokens."""
        mpn = (product.mpn or "").strip()
        if len(re.sub(r"[^A-Za-z0-9]", "", mpn)) >= 3:
            return mpn
        title = product.normalized_title or product.title or ""
        tokens = [
            t for t in re.findall(r"[a-z0-9][a-z0-9\-\.]{2,}", title.lower())
            if not t.isdigit()
        ]
        return " ".join(tokens[:3]) if tokens else None

    @staticmethod
    def _build_live_index(
        live_products: List[UnifiedProduct],
    ) -> Tuple[
        Dict[Tuple[str, str], UnifiedProduct],
        Dict[Tuple[str, str], UnifiedProduct],
        Dict[Tuple[str, str], UnifiedProduct],
    ]:
        """Indexes live results for exact matching: (vendor, external_id), URL, then SKU."""
        by_external: Dict[Tuple[str, str], UnifiedProduct] = {}
        by_url: Dict[Tuple[str, str], UnifiedProduct] = {}
        by_sku: Dict[Tuple[str, str], UnifiedProduct] = {}
        for lp in live_products:
            if lp.external_id:
                by_external[(lp.vendor, str(lp.external_id).strip())] = lp
            url_key = AggregatorEngine._offer_url_key(lp.product_url)
            if url_key:
                by_url[(lp.vendor, url_key)] = lp
            if lp.sku:
                by_sku[(lp.vendor, str(lp.sku).strip().lower())] = lp
        return by_external, by_url, by_sku

    @classmethod
    def _match_live_result(
        cls,
        product: UnifiedProduct,
        by_external: Dict[Tuple[str, str], UnifiedProduct],
        by_url: Dict[Tuple[str, str], UnifiedProduct],
        by_sku: Dict[Tuple[str, str], UnifiedProduct],
    ) -> Optional[UnifiedProduct]:
        """Finds the live listing that corresponds to a cached product, if any."""
        match = by_external.get((product.vendor, str(product.external_id).strip()))
        if match is not None:
            return match
        url_key = cls._offer_url_key(product.product_url)
        if url_key:
            match = by_url.get((product.vendor, url_key))
            if match is not None:
                return match
        if product.sku:
            match = by_sku.get((product.vendor, str(product.sku).strip().lower()))
            if match is not None:
                return match
        return None

    async def _search_vendors_for_refresh(
        self,
        query: str,
        limit_per_vendor: int,
        timeout: float,
    ) -> Tuple[List[UnifiedProduct], set, set]:
        """
        One parallel federated lookup for the refresh pass.

        Returns (live_products, ok_vendor_ids, failed_vendor_ids). A vendor that
        finishes after the budget is reported as failed rather than blocking the
        page; results from vendors that did finish in time are salvaged.
        """
        if not self.adapters:
            return [], set(), set()

        tasks: Dict["asyncio.Task", str] = {}
        for vendor_id, adapter in self.adapters.items():
            task = asyncio.create_task(adapter.search_live(query, limit=limit_per_vendor))
            # Consume exceptions in a callback so a late failure is never logged as
            # "exception was never retrieved" when the budget expires.
            task.add_done_callback(
                lambda t: t.cancelled() or t.exception()
            )
            tasks[task] = vendor_id

        try:
            done, pending = await asyncio.wait(set(tasks.keys()), timeout=max(0.5, timeout))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

        products: List[UnifiedProduct] = []
        ok: set = set()
        failed: set = set()
        for task, vendor_id in tasks.items():
            if task.cancelled():
                failed.add(vendor_id)
                continue
            try:
                resp = task.result()
            except Exception as exc:  # noqa: BLE001 - vendor failures become cache fallback
                logger.info("[Price/Stock] %s unavailable during refresh: %s", vendor_id, exc)
                failed.add(vendor_id)
                continue
            if isinstance(resp, list):
                ok.add(vendor_id)
                products.extend(resp)
            else:
                failed.add(vendor_id)
        return products, ok, failed

    async def refresh_price_stock(
        self,
        products: List[UnifiedProduct],
        query: Optional[str] = None,
        force: bool = False,
    ) -> PriceStockRefreshInfo:
        """
        Refreshes ONLY price and stock for the given products from live vendors,
        persists the result into SQLite, and falls back to the cached values when a
        live fetch is impossible (offline, rate-limited, or timing out).

        Never raises for network problems: callers always receive displayable
        products plus structured freshness metadata.
        """
        if not getattr(settings, "price_stock_refresh_enabled", True):
            return PriceStockRefreshInfo(
                status="disabled",
                cached_products=len(products),
                message="Live price/stock refresh is disabled (PRICE_STOCK_REFRESH=0).",
            )

        if not products:
            return PriceStockRefreshInfo(status="fresh_cache", message="No products to refresh.")

        now_mono = time.monotonic()
        if not force and now_mono < self._refresh_cooldown_until:
            remaining = int(self._refresh_cooldown_until - now_mono)
            return PriceStockRefreshInfo(
                status="offline_cooldown",
                cached_products=len(products),
                message=(
                    "Vendors were unreachable moments ago; serving price/stock from the local "
                    f"catalog (retrying in ~{remaining}s)."
                ),
            )

        if self._refresh_in_progress:
            return PriceStockRefreshInfo(
                status="fresh_cache",
                cached_products=len(products),
                message="Another price/stock refresh is already running; serving cached values.",
            )

        await self._ensure_adapter_loop()
        self._refresh_in_progress = True
        try:
            return await self._refresh_price_stock_inner(products, query=query, force=force)
        except Exception as exc:  # noqa: BLE001 - cache fallback must survive any failure
            logger.warning("[Price/Stock] Refresh failed unexpectedly: %s", exc)
            self._refresh_cooldown_until = time.monotonic() + float(
                getattr(settings, "price_stock_offline_cooldown_seconds", 60.0)
            )
            return PriceStockRefreshInfo(
                status="error",
                attempted=True,
                cached_products=len(products),
                message=f"Live price/stock refresh failed ({exc}); serving cached values.",
            )
        finally:
            self._refresh_in_progress = False

    async def _refresh_price_stock_inner(
        self,
        products: List[UnifiedProduct],
        query: Optional[str],
        force: bool,
    ) -> PriceStockRefreshInfo:
        candidates = [p for p in products if p.id is not None]
        info = PriceStockRefreshInfo(
            candidates=len(candidates),
            cached_products=len(products) - len(candidates),
        )
        if not candidates:
            info.status = "fresh_cache"
            info.message = "Products carry no database IDs; serving cached price/stock."
            return info

        ttl = int(getattr(settings, "price_stock_ttl_seconds", 180))
        stale_ids = set(
            db.get_stale_price_stock_ids([p.id for p in candidates], ttl, force=force)
        )
        pending = [p for p in candidates if p.id in stale_ids]

        if not pending:
            info.status = "fresh_cache"
            info.cached_products = len(products)
            info.message = f"Cached price/stock is fresh (verified within the last {ttl}s)."
            return info

        max_queries = max(1, int(getattr(settings, "price_stock_max_refresh_queries", 2)))
        budget = float(getattr(settings, "price_stock_refresh_timeout_seconds", 8.0))
        limit_per_vendor = max(1, int(getattr(settings, "price_stock_live_limit_per_vendor", 15)))

        # The user's own query is the best lookup key; products it misses fall back
        # to their MPN (or leading title tokens), bounded by the query budget.
        text_query = (query or "").strip()
        queries: List[str] = []
        if text_query:
            queries.append(text_query)
        seen = {q.lower() for q in queries}
        for p in pending:
            if len(queries) >= max_queries:
                break
            derived = self._derive_refresh_query(p)
            if derived and derived.lower() not in seen:
                seen.add(derived.lower())
                queries.append(derived)

        if not queries:
            info.status = "cached"
            info.cached_products = len(products)
            info.message = "No usable query to refresh price/stock; serving cached values."
            return info

        vendors_ok: set = set()
        vendors_failed: set = set()
        matched: Dict[int, UnifiedProduct] = {}
        used_queries: List[str] = []
        deadline = time.monotonic() + budget

        for q in queries:
            if not pending:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0.5:
                logger.info("[Price/Stock] Refresh budget exhausted before lookup %r", q)
                break

            live_products, ok, failed = await self._search_vendors_for_refresh(
                q, limit_per_vendor, remaining
            )
            used_queries.append(q)
            vendors_ok |= ok
            vendors_failed |= failed

            if live_products:
                by_external, by_url, by_sku = self._build_live_index(live_products)
                still_pending: List[UnifiedProduct] = []
                for p in pending:
                    match = self._match_live_result(p, by_external, by_url, by_sku)
                    if match is not None:
                        matched[p.id] = match
                    else:
                        still_pending.append(p)
                pending = still_pending

            if not ok:
                # Every vendor failed for this lookup: stop retrying and serve cache.
                break

        fetched_at = db.utc_now_iso()
        updates: List[Dict[str, Any]] = []
        rejected_ids: List[int] = []
        for p in candidates:
            live = matched.get(p.id)
            if live is None:
                continue
            try:
                new_price = float(live.price)
            except (TypeError, ValueError):
                rejected_ids.append(p.id)
                continue
            if new_price <= 0:
                rejected_ids.append(p.id)
                continue
            # Guard against a parser/page-layout regression silently replacing a
            # real price with a fragment (the "₹216 -> ₹37" discount-badge bug).
            if p.price >= 100 and new_price < p.price * 0.30:
                logger.warning(
                    "[Price/Stock] Ignoring implausible drop for %s (%s): %.2f -> %.2f",
                    p.id, p.title[:60], p.price, new_price,
                )
                rejected_ids.append(p.id)
                continue
            p.price = new_price
            p.available = bool(live.available)
            p.price_source = "live"
            p.price_updated_at = fetched_at
            # Carry vendor price-basis metadata (e.g. Robokits' "ex-GST + 18%")
            # so a refreshed response explains the number it is showing.
            if getattr(live, "price_note", None):
                p.price_note = live.price_note
            updates.append(
                {
                    "id": p.id,
                    "price": p.price,
                    "available": p.available,
                    "fetched_at": fetched_at,
                }
            )

        applied = db.update_price_stock_batch(updates)
        if updates and applied < len(updates):
            logger.warning(
                "[Price/Stock] %d refresh(es) could not be persisted (products missing?)",
                len(updates) - applied,
            )

        unmatched_ids = [p.id for p in candidates if p.id not in matched] + rejected_ids
        if unmatched_ids:
            # Record the attempt even when it failed, so an offline catalog is not
            # hammered on every page view until the TTL elapses.
            db.mark_price_stock_attempted(unmatched_ids, attempted_at=fetched_at)

        info.attempted = True
        info.queries = used_queries
        info.vendors_ok = sorted(vendors_ok)
        info.vendors_failed = sorted(vendors_failed)
        info.refreshed_products = applied
        info.cached_products = max(len(products) - applied, 0)
        info.verified_pct = round(
            applied / max(len(products), 1) * 100.0, 1
        )

        if applied > 0:
            info.fetched_at = fetched_at
            # 'live' must mean every returned listing was verified. Previously a
            # single refreshed row out of twenty still reported status='live',
            # and agents quoted the other nineteen cached prices as fresh.
            if applied < len(products) or vendors_failed:
                info.status = "partial"
                info.message = (
                    f"Refreshed price/stock for {applied} of {len(products)} listing(s) "
                    f"({info.verified_pct}% verified)"
                    + (
                        f"; unreachable vendors: {', '.join(sorted(vendors_failed))}."
                        if vendors_failed
                        else "."
                    )
                    + " Remaining listings use cached values."
                )
            else:
                info.status = "live"
                info.message = (
                    f"Refreshed price/stock for all {applied} listing(s) from live vendor data "
                    f"(cached in the local catalog for offline fallback)."
                )
        else:
            info.status = "cached"
            if vendors_ok:
                info.message = (
                    "Live vendors responded but no listing matched the refresh lookups; "
                    "serving cached price/stock."
                )
            else:
                self._refresh_cooldown_until = time.monotonic() + float(
                    getattr(settings, "price_stock_offline_cooldown_seconds", 60.0)
                )
                info.message = (
                    "Live vendors are unreachable; serving the latest price/stock cached in the "
                    "local catalog."
                )
        return info

    # ------------------------------------------------------------------
    # Pagination helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pool_size(page: int, page_size: int) -> int:
        """
        Products to cluster for one page.

        The pool must not grow with the page number for early pages, otherwise a
        deeper page re-clusters a larger set and cards already shown on page 1
        can move or duplicate. A fixed 200-listing window (up to MAX_GROUPING_ITEMS)
        keeps pages 1-10 stable; only very deep pages expand it.
        """
        base = min(MAX_GROUPING_ITEMS, max(200, page_size * 6))
        if page * page_size >= base:
            base = min(MAX_GROUPING_ITEMS, page * page_size * 3)
        return max(page_size, base)

    @classmethod
    def _slice_page(
        cls,
        groups: List[ComparisonGroup],
        products: List[UnifiedProduct],
        page: int,
        page_size: int,
        total_matches: int,
    ) -> Tuple[List[ComparisonGroup], List[UnifiedProduct], int, int, bool, bool]:
        start = (page - 1) * page_size
        page_groups = groups[start:start + page_size]
        wanted = {
            (offer.vendor, str(offer.external_id))
            for group in page_groups
            for offer in group.offers
        }
        page_products = [
            p for p in products if (p.vendor, str(p.external_id)) in wanted
        ]
        total_groups = len(groups)
        # Only advertise a page count once every matching listing was clustered:
        # before that the pool is a window and the count would keep growing.
        exhausted = len(products) >= total_matches
        total_pages = (
            (total_groups + page_size - 1) // page_size
            if exhausted and total_groups
            else 0
        )
        more_in_pool = (start + page_size) < total_groups
        more_in_catalog = not exhausted and len(products) < MAX_GROUPING_ITEMS
        has_next = more_in_pool or more_in_catalog
        return page_groups, page_products, total_groups, total_pages, has_next, page > 1

    @classmethod
    def _page_response(
        cls,
        query: str,
        page: int,
        page_size: int,
        groups: List[ComparisonGroup],
        products: List[UnifiedProduct],
        total_matches: int,
        price_refresh: Optional[PriceStockRefreshInfo] = None,
        source: str = "local_db",
        query_contract: Optional[Dict[str, Any]] = None,
    ) -> SearchResponse:
        (
            page_groups,
            page_products,
            total_groups,
            total_pages,
            has_next,
            has_prev,
        ) = cls._slice_page(groups, products, page, page_size, total_matches)
        return SearchResponse(
            query=query,
            total_results=total_matches,
            source=source,
            query_contract=query_contract or {},
            results=page_products,
            comparison_groups=page_groups,
            price_refresh=price_refresh,
            page=page,
            page_size=page_size,
            total_groups=total_groups,
            total_pages=total_pages,
            returned_results=len(page_products),
            has_next=has_next,
            has_prev=has_prev,
        )

    # ------------------------------------------------------------------
    # Search-time price/stock refresh (continued)
    # ------------------------------------------------------------------

    def _schedule_background_refresh(
        self, products: List[UnifiedProduct], query: Optional[str]
    ) -> bool:
        """
        Kicks off a non-blocking live price/stock refresh for one page of listings.

        Search must not wait seconds for five storefronts before painting a page.
        The refreshed values are persisted to SQLite, and the client re-reads the
        page once (`refresh=off`) after the badge announces the refresh.
        """
        if not getattr(settings, "price_stock_refresh_enabled", True):
            return False
        if self._refresh_in_progress:
            return False
        ids = [p.id for p in products if p.id is not None]
        if not ids:
            return False

        # Claim the slot synchronously so two concurrent requests cannot both
        # start a refresh in the gap before the task runs.
        self._refresh_in_progress = True
        task = asyncio.create_task(self._run_background_refresh(ids, query))
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)
        return True

    async def _run_background_refresh(self, product_ids: List[int], query: Optional[str]) -> None:
        try:
            await self._ensure_adapter_loop()
            # Re-read so the background task never mutates objects owned by a
            # response that is being serialized.
            products = await asyncio.to_thread(db.get_products_by_ids, product_ids)
            await self._refresh_price_stock_inner(products, query=query, force=False)
        except Exception as exc:  # noqa: BLE001 - background refresh must never crash the app
            logger.warning("[Price/Stock] Background refresh failed: %s", exc)
            self._refresh_cooldown_until = time.monotonic() + float(
                getattr(settings, "price_stock_offline_cooldown_seconds", 60.0)
            )
        finally:
            self._refresh_in_progress = False

    async def search_local_refreshed(
        self,
        query: str,
        limit: int = 50,
        vendor: Optional[str] = None,
        in_stock_only: bool = False,
        refresh_mode: str = "auto",
        category: Optional[str] = None,
        spec_filters: Optional[Dict[str, Any]] = None,
        price_min: Optional[float] = None,
        price_max: Optional[float] = None,
        page: int = 1,
        page_size: Optional[int] = None,
        background_refresh: bool = False,
    ) -> SearchResponse:
        """
        Local catalog search with group-level pagination and price/stock refresh.

        refresh_mode: 'auto' (respect the TTL), 'force' (bypass the TTL), 'off'.
        page/page_size: which page of comparison cards to return.
        background_refresh: when True (web UI), a stale price refresh runs after
        the response instead of blocking it; the client re-reads once it lands.
        """
        page = max(1, int(page or 1))
        requested = page_size if page_size is not None else limit
        page_size = max(1, min(int(requested or settings.search_page_size), settings.search_page_size_max))
        pool = self._pool_size(page, page_size)

        batch = await asyncio.to_thread(
            retrieve_catalog, query=query, limit=pool, vendor=vendor,
            in_stock_only=in_stock_only, category=category, spec_filters=spec_filters,
            price_min=price_min, price_max=price_max,
        )
        total_matches, products = batch.total_matches, batch.products
        groups = await asyncio.to_thread(self.group_and_compare, products, query=query)

        page_groups, page_products, _, _, _, _ = self._slice_page(
            groups, products, page, page_size, total_matches
        )

        price_refresh: Optional[PriceStockRefreshInfo] = None
        if page_products and refresh_mode != "off":
            sync_limit = max(1, int(getattr(settings, "price_stock_sync_max_products", 30)))
            if (
                background_refresh
                and refresh_mode == "auto"
                and getattr(settings, "price_stock_background_refresh", True)
            ):
                if self._schedule_background_refresh(page_products, query):
                    price_refresh = PriceStockRefreshInfo(
                        status="refreshing",
                        cached_products=len(page_products),
                        message=(
                            "Showing cached prices while live prices are verified in the "
                            "background; the page refreshes automatically when done."
                        ),
                    )
                else:
                    price_refresh = PriceStockRefreshInfo(
                        status="fresh_cache",
                        cached_products=len(page_products),
                        message="A live price refresh is already running; serving cached values.",
                    )
            elif refresh_mode == "force" or len(page_products) <= sync_limit:
                price_refresh = await self.refresh_price_stock(
                    page_products, query=query, force=(refresh_mode == "force")
                )
                if price_refresh.refreshed_products:
                    # Prices/stock changed: rebuild so best-price badges and stock
                    # filters reflect the verified values.
                    groups = await asyncio.to_thread(
                        self.group_and_compare, products, query=query
                    )
            else:
                price_refresh = PriceStockRefreshInfo(
                    status="cached",
                    cached_products=len(page_products),
                    message=(
                        "Broad search: showing cached prices. Use refresh=force to verify "
                        "this page live, or narrow the query."
                    ),
                )

        return self._page_response(
            query=query,
            page=page,
            page_size=page_size,
            groups=groups,
            products=products,
            total_matches=total_matches,
            price_refresh=price_refresh,
            query_contract=batch.query_contract,
        )

    async def browse_local(
        self,
        page: int = 1,
        page_size: int = 20,
        category: Optional[str] = None,
        vendor: Optional[str] = None,
        in_stock_only: bool = False,
        spec_filters: Optional[Dict[str, Any]] = None,
        price_min: Optional[float] = None,
        price_max: Optional[float] = None,
    ) -> SearchResponse:
        """Paginated category/vendor browse (no text query)."""
        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or settings.search_page_size), settings.search_page_size_max))
        pool = self._pool_size(page, page_size)

        total_matches = await asyncio.to_thread(
            db.count_products_by_category,
            category=category or None,
            vendor=vendor or None,
            in_stock_only=bool(in_stock_only),
            spec_filters=spec_filters or None,
            price_min=price_min,
            price_max=price_max,
        )
        products = await asyncio.to_thread(
            db.list_products_by_category,
            category=category or None,
            vendor=vendor or None,
            in_stock_only=bool(in_stock_only),
            limit=pool,
            spec_filters=spec_filters or None,
            price_min=price_min,
            price_max=price_max,
        )
        groups = await asyncio.to_thread(self.group_and_compare, products, None)
        return self._page_response(
            query="",
            page=page,
            page_size=page_size,
            groups=groups,
            products=products,
            total_matches=total_matches,
        )


    async def search_live(self, query: str, limit_per_vendor: int = 15) -> SearchResponse:
        """
        Federated search across Robu, QuartzComponents, and MakerBazar in parallel.
        Automatically saves/enriches products into local SQLite database.
        """
        clean_q = query.strip()
        if not clean_q:
            return SearchResponse(query=query, total_results=0, source="live")

        await self._ensure_adapter_loop()

        tasks = [
            adapter.search_live(clean_q, limit=limit_per_vendor)
            for adapter in self.adapters.values()
        ]

        responses = await asyncio.gather(*tasks, return_exceptions=True)

        all_products: List[UnifiedProduct] = []
        vendors_ok: List[str] = []
        vendors_failed: List[str] = []
        for (name, adapter), resp in zip(self.adapters.items(), responses):
            if isinstance(resp, Exception):
                logger.error(f"Error querying {name} live: {resp}")
                vendors_failed.append(name)
            elif isinstance(resp, list):
                vendors_ok.append(name)
                all_products.extend(resp)

        fetched_at = db.utc_now_iso()
        for p in all_products:
            p.price_source = "live"
            p.price_updated_at = fetched_at

        # Upsert newly fetched live items to local DB in background
        if all_products:
            try:
                db.upsert_products(all_products)
            except Exception as e:
                logger.warning(f"Error updating local database with live search: {e}")

        # Vendor search engines may relax independently. Reapply our own
        # eligibility before exposing a listing as a confirmed result.
        connection = db.get_db_connection()
        try:
            ids = []
            for product in all_products:
                row = connection.execute(
                    "SELECT id FROM products WHERE vendor = ? AND external_id = ?",
                    (product.vendor, str(product.external_id)),
                ).fetchone()
                if row:
                    ids.append(int(row[0]))
        finally:
            connection.close()
        batch = await asyncio.to_thread(retrieve_catalog, clean_q,
                                        limit=max(1, len(ids)), listing_ids=tuple(ids))
        eligible = {(product.vendor, str(product.external_id)): product.id for product in batch.products}
        all_products = [product for product in all_products
                        if (product.vendor, str(product.external_id)) in eligible]
        for product in all_products:
            product.id = eligible[(product.vendor, str(product.external_id))]

        comparison_groups = self.group_and_compare(all_products)

        if all_products:
            refresh_status = "partial" if vendors_failed else "live"
            refresh_message = (
                f"Live vendor search returned {len(all_products)} listing(s); "
                f"price/stock cached in the local catalog."
            )
        elif vendors_ok:
            refresh_status = "live"
            refresh_message = "Live vendors responded but returned no matching listings."
        else:
            refresh_status = "cached"
            refresh_message = "No vendor could be reached; no live listings to compare."

        price_refresh = PriceStockRefreshInfo(
            status=refresh_status,
            attempted=True,
            candidates=len(all_products),
            refreshed_products=len(all_products),
            cached_products=0,
            vendors_ok=sorted(vendors_ok),
            vendors_failed=sorted(vendors_failed),
            queries=[clean_q],
            fetched_at=fetched_at if all_products else None,
            message=refresh_message,
        )

        return SearchResponse(
            query=clean_q,
            total_results=len(all_products),
            source="live",
            query_contract=batch.query_contract,
            results=all_products,
            comparison_groups=comparison_groups,
            price_refresh=price_refresh,
            page=1,
            page_size=max(1, len(comparison_groups)),
            total_groups=len(comparison_groups),
            total_pages=1 if comparison_groups else 0,
            returned_results=len(all_products),
            has_next=False,
            has_prev=False,
        )

    def search_local(
        self,
        query: str,
        limit: int = 50,
        vendor: Optional[str] = None,
        in_stock_only: bool = False,
        category: Optional[str] = None,
        spec_filters: Optional[Dict[str, Any]] = None,
        price_min: Optional[float] = None,
        price_max: Optional[float] = None,
    ) -> SearchResponse:
        """
        Queries indexed database using Enhanced SQLite BM25 + RRF and intent-gated ranking.

        Synchronous and cache-only: use `search_local_refreshed` (async) when the
        response should carry freshly verified price/stock with fallback.
        """
        batch = retrieve_catalog(
            query=query,
            limit=limit,
            vendor=vendor,
            in_stock_only=in_stock_only,
            category=category,
            spec_filters=spec_filters,
            price_min=price_min,
            price_max=price_max,
        )
        products = batch.products
        comparison_groups = self.group_and_compare(products, query=query)
        return SearchResponse(
            query=query,
            total_results=batch.total_matches,
            query_contract=batch.query_contract,
            source="local_db",
            results=products,
            comparison_groups=comparison_groups
        )


    async def run_vendor_ingest(
        self,
        vendor_id: str,
        max_pages: Optional[int] = None,
        resume: bool = True
    ) -> AsyncGenerator[IngestProgress, None]:
        """
        Continuable catalog ingestion for a single vendor.
        If interrupted and resumed, picks up from checkpoint.last_page + 1.
        """
        if vendor_id not in self.adapters:
            raise ValueError(f"Unknown vendor '{vendor_id}'. Supported: {list(self.adapters.keys())}")

        adapter = self.adapters[vendor_id]
        checkpoint = db.get_checkpoint(vendor_id)

        # Determine starting page
        if resume and checkpoint.last_page > 0 and checkpoint.status != "completed":
            start_page = checkpoint.last_page + 1
            total_ingested = checkpoint.total_ingested
            logger.info(f"[{adapter.vendor_name}] Resuming scraping from page {start_page} (already ingested: {total_ingested})")
        else:
            if not resume:
                db.reset_checkpoint(vendor_id)
            start_page = 1
            total_ingested = 0
            logger.info(f"[{adapter.vendor_name}] Starting fresh ingestion from page 1")

        current_page = start_page
        pages_processed = 0

        db.save_checkpoint(
            vendor=vendor_id,
            last_page=current_page - 1,
            last_offset=0,
            total_ingested=total_ingested,
            status="in_progress"
        )

        try:
            while True:
                if max_pages is not None and pages_processed >= max_pages:
                    logger.info(f"[{adapter.vendor_name}] Reached requested limit of {max_pages} pages. Pausing session.")
                    db.save_checkpoint(
                        vendor=vendor_id,
                        last_page=current_page - 1,
                        last_offset=0,
                        total_ingested=total_ingested,
                        status="paused"
                    )
                    yield IngestProgress(
                        vendor=vendor_id,
                        current_page=current_page - 1,
                        pages_processed=pages_processed,
                        items_added=0,
                        total_vendor_items=total_ingested,
                        is_complete=False,
                        message=f"Reached session limit of {max_pages} pages. Scrape checkpoint saved."
                    )
                    break

                # Fetch one page
                products, has_more = await adapter.fetch_catalog_page(page=current_page)
                items_added = 0
                if products:
                    items_added = db.upsert_products(products)
                    total_ingested += items_added

                pages_processed += 1
                # Save checkpoint after each successfully saved page
                db.save_checkpoint(
                    vendor=vendor_id,
                    last_page=current_page,
                    last_offset=0,
                    total_ingested=total_ingested,
                    status="in_progress" if has_more else "completed"
                )

                yield IngestProgress(
                    vendor=vendor_id,
                    current_page=current_page,
                    pages_processed=pages_processed,
                    items_added=items_added,
                    total_vendor_items=total_ingested,
                    is_complete=not has_more,
                    message=f"Page {current_page} indexed (+{items_added} items)."
                )

                if not has_more:
                    logger.info(f"[{adapter.vendor_name}] Catalog finished at page {current_page}.")
                    db.save_checkpoint(
                        vendor=vendor_id,
                        last_page=current_page,
                        last_offset=0,
                        total_ingested=total_ingested,
                        status="completed"
                    )
                    break

                current_page += 1

        except Exception as e:
            logger.error(f"[{adapter.vendor_name}] Exception during ingestion on page {current_page}: {e}")
            db.save_checkpoint(
                vendor=vendor_id,
                last_page=current_page - 1,
                last_offset=0,
                total_ingested=total_ingested,
                status="error",
                last_error=str(e)
            )
            raise
