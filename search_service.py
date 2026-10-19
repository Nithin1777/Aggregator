"""
Aggregator search service: multi-vendor live federation, local FTS, side-by-side comparison clustering,
and continuable catalog ingestion.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import sqlite3
from typing import AsyncGenerator, Dict, List, Optional, Tuple
from adapters.base import BaseAdapter
from adapters.robu import RobuAdapter
from adapters.robokits import RobokitsAdapter
from adapters.shopify import ShopifyAdapter
from config import settings
import db
from models import (
    ComparisonGroup,
    IngestProgress,
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
from src.search.retrieval import search_catalog_ranked
from gray_zone import evaluate_gray_zone_risk, record_gray_zone_entry



logger = logging.getLogger(__name__)

class AggregatorEngine:
    """Core aggregation and comparison engine for Indian electronic component vendors."""

    def __init__(self):
        # Initialize vendor adapters
        self.adapters: Dict[str, BaseAdapter] = {
            "robu": RobuAdapter(),
            "quartzcomponents": ShopifyAdapter(
                vendor_id="quartzcomponents",
                vendor_name="Quartz Components",
                base_url=settings.quartz_base_url
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
        """
        # 1. Enrich products with ComponentItem
        tagged_items: List[Tuple[UnifiedProduct, ComponentItem]] = []
        for p in products:
            ci = ComponentItem(p.title, p.price, mpn=p.mpn)
            tagged_items.append((p, ci))

        # 2. Strict grouping by evaluate_pair_merge
        clusters: List[Dict[str, Any]] = []

        for p, ci in tagged_items:
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
                    merge_reason=merge_reason,
                    variant_key=variant_key,
                    source=card_source
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

        # Sort groups: multi-vendor matches first, then in-stock count, then title
        result.sort(key=lambda g: (-g.total_offers, -g.in_stock_count, g.canonical_name))
        return result

    async def search_live(self, query: str, limit_per_vendor: int = 15) -> SearchResponse:
        """
        Federated search across Robu, QuartzComponents, and MakerBazar in parallel.
        Automatically saves/enriches products into local SQLite database.
        """
        clean_q = query.strip()
        if not clean_q:
            return SearchResponse(query=query, total_results=0, source="live")

        tasks = [
            adapter.search_live(clean_q, limit=limit_per_vendor)
            for adapter in self.adapters.values()
        ]

        responses = await asyncio.gather(*tasks, return_exceptions=True)

        all_products: List[UnifiedProduct] = []
        for (name, adapter), resp in zip(self.adapters.items(), responses):
            if isinstance(resp, Exception):
                logger.error(f"Error querying {name} live: {resp}")
            elif isinstance(resp, list):
                all_products.extend(resp)

        # Upsert newly fetched live items to local DB in background
        if all_products:
            try:
                db.upsert_products(all_products)
            except Exception as e:
                logger.warning(f"Error updating local database with live search: {e}")

        comparison_groups = self.group_and_compare(all_products)

        return SearchResponse(
            query=clean_q,
            total_results=len(all_products),
            source="live",
            results=all_products,
            comparison_groups=comparison_groups
        )

    def search_local(
        self,
        query: str,
        limit: int = 50,
        vendor: Optional[str] = None,
        in_stock_only: bool = False
    ) -> SearchResponse:
        """
        Queries indexed database using Enhanced SQLite BM25 + RRF and intent-gated ranking.
        """
        products = search_catalog_ranked(
            query=query,
            limit=limit,
            vendor=vendor,
            in_stock_only=in_stock_only
        )
        comparison_groups = self.group_and_compare(products, query=query)
        return SearchResponse(
            query=query,
            total_results=len(products),
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
