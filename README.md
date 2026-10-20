# Aggregate (Indian Component Aggregator Engine)

A high-performance component aggregation search engine that indexes, queries, and compares electronic component pricing and live stock availability across India's primary distributors:
1. **Robu.in**
2. **QuartzComponents**
3. **MakerBazar**
4. **Robokits**
5. **REES52**

Built with direct JSON querying (zero HTML scraping required), Cloudflare TLS/JA3 fingerprint impersonation (`curl_cffi` on `chrome120`), SQLite Full-Text Search (FTS5), smart MPN extraction, cross-vendor component clustering, and **resumable / continuable batch catalog ingestion**.

---

## Architecture Overview

```text
├── config.py             # Global constants, timeouts, URLs, and DB settings
├── models.py             # Unified Pydantic schemas (UnifiedProduct, ComparisonGroup, etc.)
├── db.py                 # SQLite persistence with FTS5 virtual tables and checkpoint tracking
├── normalizer.py         # MPN / IC extractor (ESP32, STM32, LM2596...) and title normalizer
├── adapters/
│   ├── __init__.py
│   ├── base.py           # Abstract BaseAdapter interface
│   ├── shopify.py        # Shopify adapter (QuartzComponents, MakerBazar)
│   └── robu.py           # Robu Next.js search microservice adapter with session warmup
├── search_service.py     # Parallel federation, comparison clustering, price/stock refresh, and continuable ingestion
├── main.py               # Typer CLI dashboard and FastAPI Web Application
├── src/
│   ├── search/query_terms.py  # Shared query understanding (stop words, AND->OR relaxation, typo candidates)
│   ├── web/                   # Inline SVG icon pack and render helpers
│   └── spec/             # Taxonomy loader, product classifier, spec extractor and shared filters
├── config/
│   ├── taxonomy.yaml     # THE taxonomy: 32 categories, filterable attributes, matching rules
│   └── extractors.yaml   # Regex extractors with load-time tests
├── mcp_server/
│   └── server.py         # Agent-first MCP server (search, detail, MPN lookup) with the same price/stock refresh
├── scripts/
│   ├── backfill_categories.py # Applies the canonical taxonomy to the catalog
│   └── generate_icons.py      # Regenerates src/web/icons.py from lucide-static
├── static/
│   ├── css/aggregate.css  # Design system for the web UI
│   ├── js/aggregate.js    # Type-ahead search, filters, result rendering
│   └── icons/favicon.svg
├── templates/
│   ├── base.html         # Shared shell (header, footer, lightbox)
│   ├── index.html        # Landing + search results (SPA view switching)
│   ├── categories.html   # Category index
│   ├── vendors.html      # Vendor index
│   └── partials/search_box.html
├── tests/                # Comprehensive test suite (100+ unit and integration tests)
└── requirements.txt
```

---

## 1. Quick Start

Activate the virtual environment:
```powershell
.\.venv\Scripts\activate
```

Run unit tests:
```powershell
python -m pytest tests -q
```
(Use the `tests` path: `scratch/` contains standalone probe scripts that are not part of the suite.)

---

## 2. Searching Components

### A. Real-Time Multi-Vendor Search (Live APIs)
Queries Robu, QuartzComponents, and MakerBazar simultaneously in parallel, normalizes titles, groups equivalent parts side-by-side, highlights the lowest in-stock price, and automatically caches products to your local database:

```powershell
python main.py search "esp32"
python main.py search "mpu6050"
python main.py search "lm2596"
python main.py search "arduino uno"
```

### B. Instant Local Search (Sub-millisecond Offline FTS5)
Search your locally ingested component database instantly using SQLite Full-Text Search:

```powershell
python main.py search "stm32" --local
python main.py search "sensor" --local
```

Local search automatically re-verifies **price and stock** from live vendors before display (see [Section 5](#5-search-time-pricestock-refresh-live-values-offline-fallback)). Add `--cache-only` to skip the network and use cached values only:

```powershell
python main.py search "IG52 motor" --local --cache-only
```

---

## 3. Continuable Scraping & Checkpointing (Safe for Overnight Runs)

The ingestion engine stores progress in SQLite (`scrape_checkpoints` table). Every page is committed and checkpointed immediately.

- **You can safely stop (Ctrl+C) anytime.**
- When you run `ingest` again, it **automatically detects the last scraped page and resumes from the next un-scraped page**.
- You can test with a few pages using `-m / --max-pages` or omit the flag to let it run continuously overnight.

### Ingestion Commands:

```powershell
# 1. Ingest a specific vendor for 2 pages (for quick testing)
python main.py ingest quartzcomponents -m 2
python main.py ingest makerbazar -m 2
python main.py ingest robu -m 2

# 2. Check current status, checkpoint pages & Robu 100% Parity Tracker
python main.py status

# 3. Optional: Sync all 22 Robu ground-truth sitemaps (~22,000 URLs)
python main.py robu-sync

# 4. Resume ingestion anytime (picks up exactly from checkpoint.last_page + 1)
python main.py ingest quartzcomponents -m 5

# 5. Leave running overnight (omitting -m processes until full catalog is completed)
python main.py ingest quartzcomponents
python main.py ingest makerbazar
python main.py ingest robu

# 6. Ingest all 3 vendors sequentially
python main.py ingest

# 7. Optional: Start fresh from page 1 instead of resuming
python main.py ingest quartzcomponents --fresh
```

---

## 4. Web UI & REST API

### One command: `aggregate`

The launcher is installed at `C:\Users\nithi\.local\bin\aggregate.cmd`, which is already on `PATH`:

```powershell
aggregate                 # web on :8000 + MCP SSE on :8001, opens the browser
aggregate --web           # web only
aggregate --mcp           # MCP SSE only (http://127.0.0.1:8001/sse)
aggregate --mcp --stdio   # MCP stdio (for MCP client configs)
aggregate --help --mcp    # full MCP connection guide (also --mcp-help)
aggregate --list-tools    # print the exposed MCP tools and exit
aggregate --port 8010 --mcp-port 8011 --no-open   # custom ports, no browser
```

`aggregate` detects ports that are already in use instead of starting a duplicate, and `Ctrl+C` stops the processes it started. `--mcp --stdio` keeps stdin/stdout clean for MCP clients. On a fresh checkout, recreate the launcher with `python scripts/install_aggregate.py`.

Launch the web application directly (alternative):
```powershell
python main.py serve --port 8000
```
Open your browser at **http://localhost:8000**:
- Minimal, responsive landing page with type-ahead suggestions.
- One shared category taxonomy across every vendor (32 categories, live counts).
- Category, vendor and in-stock filters that combine with the text query.
- Side-by-side comparison tables with live stock badges and a lowest-price badge.
- Relevance-ranked results paginated at the comparison-card level (20 cards per page, Prev/Next pager). Irrelevant long-tail matches stay on later pages instead of bloating page 1.
- Prices are shown tax-inclusive; Robokits and QuartzComponents quote ex-GST prices, so 18% GST is added before comparison. Only the final inclusive price is displayed (no per-vendor tax breakdown).
- Browse mode: `/?category=resistors` lists a category without typing a query.
- Dedicated pages for `/categories` and `/vendors`.

### JSON API Endpoints:
- `GET /api/search?q={query}&source=live` — Multi-vendor search JSON with comparison groups.
- `GET /api/search?q={query}&source=local&page=1&page_size=20` — Paginated local search JSON. `page` is 1-based; `page_size` defaults to 20 (max 50). The response carries `page`, `page_size`, `total_results`, `total_groups`, `total_pages`, `has_next` and `has_prev`. `limit` is still accepted as a legacy alias for `page_size`.
- `GET /api/search?q={query}&source=local` — Local indexed search JSON; verifies price/stock without blocking the first paint. Add `&refresh=auto|force|off` (default `auto`).
- `GET /api/search?q=&category={slug}&page=2` — Browse mode: paginate a category without a text query.
- `GET /api/search?q={query}&category={slug}&vendor={slug}&in_stock=1` — Combine the query with filters.
- `GET /api/search?q={query}&price_min=100&price_max=500` — Price bounds in INR. A budget written in the query itself (`resistor under ₹500`, `motor 300 to 1200`, `esp32 over 300`) is parsed out of the text and applied the same way.
- `GET /api/search?q={query}&spec_filters={"rpm":{"lte":1000}}` — Spec-attribute filters (voltage, rpm, capacity_mah, chemistry, ...). Same semantics as the MCP `filters=` argument.
- `GET /api/suggest?q={prefix}&limit=8` — Type-ahead suggestions (part numbers, products, categories).
- `GET /api/categories?vendor={slug}` — The taxonomy with live part counts and the filterable attributes of each category.
- `GET /api/popular?limit=8` — The most searched queries so far.
- `GET /api/status` — Database item counts, in-stock counts, scrape checkpoint statuses, and price/stock refresh stats.
- `GET /health` — Lightweight liveness probe.
- `POST /api/ingest?vendor={vendor}&max_pages={n}&resume=true` — Trigger background catalog ingestion.

### Ranking and pagination
The same ranking pipeline runs for **web search and the MCP `search_components` text path** (`src/search/retrieval.py::rank_products`), so an agent and a browser get the same ordering.

1. **Retrieval order** — SQLite FTS5 BM25 (`ORDER BY rank`), after strict/typo-corrected/relaxed query resolution.
2. **Listing score** — RRF base (`10 * (1 + 50/(60+position+1))`) plus:
   - canonical boost (+25 brand/category, +35 exact MPN, +2 other),
   - gentle length penalty (`1 / (1 + 0.035 * max(0, tokens-5))`),
   - keyword repetition penalty (−4 per repeat),
   - **product-role/attachment affinity** (`src/search/product_prior.py`): a title that *is* the part beats a title that merely mentions it. It detects accessories/consumables/enclosures (`bracket`, `brush`, `cooler`, `case`, …), kits/bundles, query text appearing only after `for …` / `with …`, and core component nouns. Prefix matching lets `stm32` match `STM32F103C8T6`.
   - **taxonomy category hint** — a bare category/domain query (`display`, `led`, `battery`, `raspberry pi 5`, `nema17 stepper`) boosts products in that canonical category; accessory queries such as `battery holder` and ambiguous terms are left alone.
3. **Group score** — cards inherit the position of their best listing, then receive:
   - exact-token (+8), contiguous-phrase (+4), role-affinity (−4 × affinity), category (+6);
   - **multi-vendor depth**: −4 per vendor offer (max −12) for broad queries, −3 per offer (max −9) for exact MPN lookups;
   - **stock**: in-stock cards get −4 (−2 for exact MPN), cards with zero purchasable offers get +2 (+1 for exact MPN);
   - **bundle/kit penalty**: a kit/combo card is pushed down for a bare component query, unless the query itself asks for a kit/set/combo.
4. **Pagination** — ranking is global; pages are contiguous slices. A fixed 200-listing cluster window keeps pages 1–9 stable (later pages expand to 400). `page=1` holds the strongest matches; accessories, kits and “for X” long-tail items move to pages 2, 3, … instead of pushing the real part down.

Plural/singular title variants are normalized before matching (`Kit` vs `Kits`), so they merge into one comparison card instead of producing duplicates.

---

## 5. Search-Time Price/Stock Refresh (Live Values, Offline Fallback)

Every **local** search — Web UI, `GET /api/search?source=local`, `python main.py search --local`, and the MCP tools `search_components`, `get_product`, `search_by_mpn` — re-verifies **price and stock** against the live vendors.

### Web search never blocks on the price lookup
The browser first renders the cached page in well under a second, then a background refresh runs for the listings on that page and writes the verified values to SQLite. The page does **not** re-render itself when that finishes (the old auto re-read caused a loading spinner and a scroll jump a few seconds after every search); the refreshed values are used by the next search/page/filter action. `refresh=force` performs verification synchronously if the caller wants it in the same response.

The CLI and MCP tools stay synchronous on purpose (agents need the verified value in the same response); their refreshes are bounded by the same budget.

### What gets refreshed
- Only `price` and `available` (stock) for listings already matched in the local catalog.
- Titles, specs, descriptions, images, URLs, MPNs, SKUs and all other fields are never modified.
- Refreshed values are persisted to SQLite (`products.price_stock_updated_at`, `price_stock_attempted_at`, `price_stock_source`), so the cache always holds the most recent known value.
- Refresh never inserts or removes products. Live results without a cached match are ignored.
- An implausibly large price collapse (>70% below the cached value on a listing over ₹100) is rejected and logged instead of overwriting good data with a parser fragment.

### Offline and failure behavior
- If the internet or a vendor is unavailable, times out, or rate-limits, the search still succeeds with the **last cached price/stock**.
- A short circuit breaker (default **60s**) opens after a fully failed pass so repeated offline page views stay fast.
- The response carries structured refresh metadata:
  - Web UI: a “Prices & stock: …” badge.
  - JSON: `price_refresh` (`SearchResponse`).
  - MCP: `data.price_stock_refresh`, plus warning code `price_stock_cache_fallback` when cached values were served after a failed attempt.

### TTL, matching and budgets
- Cached price/stock is re-verified only when older than the TTL (default **180s**). Fresh cache skips vendor calls entirely.
- A refresh pass is bounded (default **8s**), uses at most **2** distinct queries (your query first, then derived MPN/title lookups), and requests at most **15** results per vendor.
- Live results are matched to cached listings by `(vendor, external_id)`, then normalized product URL, then `(vendor, SKU)`.

### Refresh modes
- `auto` (default) — refresh only listings whose cache is stale.
- `force` — bypass the TTL and re-verify now (useful after a known price change).
- `off` — cache-only; make no vendor calls.

```powershell
# Web UI / API
# http://localhost:8000/api/search?q=IG52%20motor&source=local&refresh=auto
# http://localhost:8000/api/search?q=IG52%20motor&source=local&refresh=force
# http://localhost:8000/api/search?q=IG52%20motor&source=local&refresh=off

# CLI (local mode)
python main.py search "IG52 motor" --local              # auto refresh
python main.py search "IG52 motor" --local --cache-only # no vendor calls
```

### MCP refresh controls
- `search_components(..., refresh=True, force_refresh=False)`
- `get_product(product_id, refresh=True, force_refresh=False)`
- `search_by_mpn(mpn, refresh=True, force_refresh=False)`

`count_components` intentionally remains a cached-snapshot operation. MCP tools stay synchronous; refresh runs on a dedicated background event loop so the same cached-fallback behavior applies to agents.

### Refresh status values

| Status | Meaning |
|---|---|
| `live` | Price/stock refreshed from live vendors. |
| `partial` | Refreshed, but one or more vendors failed; remaining listings use cache. |
| `refreshing` | Web only: cached values were returned immediately and verification is running in the background; the page does not auto re-render. |
| `fresh_cache` | Cached values were within the TTL; no vendor call was needed. |
| `cached` | Refresh was attempted but unavailable, or no live listing matched; cached values served. |
| `offline_cooldown` | Vendors were unreachable recently; cache served without retrying. |
| `timeout` | The refresh exceeded its wall-clock budget; cached values served. |
| `error` | Unexpected refresh failure; cached values served. |
| `disabled` | Live refresh is turned off via configuration. |

### Configuration

| Env var | Default | Description |
|---|---|---|
| `PRICE_STOCK_REFRESH` | `1` | Set to `0`/`false`/`off`/`no` to disable live refresh entirely. |
| `PRICE_STOCK_TTL` | `180` | Seconds before cached price/stock is considered stale. |
| `PRICE_STOCK_TIMEOUT` | `8` | Wall-clock budget in seconds for one refresh pass. |

Additional tunables live in `config.py`: `price_stock_offline_cooldown_seconds` (60), `price_stock_max_refresh_queries` (2), `price_stock_live_limit_per_vendor` (15), `price_stock_sync_max_products` (30; broader result sets use the background/cached path), `robokits_gst_rate` (0.18), and `quartz_gst_rate` (0.18).

### Vendor tax handling
Robokits and QuartzComponents list prices **excluding GST**. The adapters multiply the scraped amount by the vendor's GST rate before storing it, so every vendor's `price` is tax-inclusive and comparisons are like-for-like. The original ex-GST amount is kept in `extra.price_ex_gst` for data/debugging use, but the storefront only displays the final inclusive price.

Historical rows can be repaired without a full re-scrape:
```powershell
python scripts/apply_vendor_gst.py --vendor quartzcomponents --rate 0.18
```
The script is idempotent (rows already carrying `extra.gst_rate` are skipped) and clears the refresh marker so the next search re-verifies the converted price.

Existing catalogs that were ingested before this change can be healed in two ways: any page viewed through search re-verifies and rewrites its visible Robokits prices, and the whole vendor can be re-ingested with `python main.py ingest robokits --fresh`.

Check cache coverage and the last successful refresh with:
```powershell
python main.py status
```
The same data is available from `GET /api/status` (`price_stock`) and the MCP `health` / `describe_catalog` tools.

---

## 6. Agent-Friendly Querying (MCP + Web)

Agents (and people) rarely type exact keywords. The same query-understanding pipeline now runs for
the **MCP tools**, the **web UI**, and `GET /api/search?source=local`, driven by
`src/search/query_terms.py` and `db.resolve_query_plan()`:

1. **Filler words are dropped** — “battery *to power* an arduino *for* 8 hours” searches
   `battery power arduino 8 hours`.
2. **All-terms matching relaxes to any-term matching** when nothing matches everything — results
   stay BM25-ranked and the response says so (`warnings: [{"code": "query_relaxed"}]`).
3. **Mistyped words are corrected from the catalog vocabulary** (FTS5 vocabulary table):
   `esp32 wromm` → `esp32 wroom`, `battry 18650` → `battery 18650`
   (`warnings: query_spelling_corrected`).
4. **Nothing resembles the query?** You still get `ok: true` with guidance:
   `warnings: query_unresolved` plus `data.query_suggestions` — never a bare empty page.

`filters_applied.query` always echoes what the caller typed; `filters_applied.query_effective`
shows what was actually searched.

**Booleans and quotes are not silently rewritten.** `esp32 OR esp8266` returns
`error_code: "unsupported_query_syntax"`; run one query per alternative instead. Lowercase
`or`/`and` are reported as `query_operator_dropped`.

**Relaxation is progressive, not all-or-nothing.** When a strict query is empty, generic
modifiers are dropped before the head term; the response exposes `query_tokens_dropped`,
`query_status` (`exact`, `relaxed`, `low_confidence`, `unresolved`) and `match_ratio_top`.
A query that matches only a handful of rows while the head term matches thousands also gets a
`query_narrowed` warning, so a plausible-looking small count is not mistaken for the full answer.

**Price intent is honoured.** “cheapest X” sets `sort=price_asc` (`sort_inferred`), price-zero
quote/fabrication rows are excluded from price sorts, and “under ₹500” is parsed into
`price_max` when the caller did not pass one.

### Component categories

`search_components`, `count_components` (and the web search) accept a `category` filter —
`battery`, `motor`, `relay`, or any taxonomy category (see `describe_catalog` → `data.taxonomy`).
The name is case-insensitive and plural tolerant (`Motor`, `motors` → `motor`); a typo returns
`error_code: "unknown_category"` with ranked nearest categories (`motr` → `motor`). Category
filters use `products.category_canonical`, not the spec-extraction scope, so a 9V battery snap
connector filed under “Battery Accessories” cannot enter a `category="battery"` result through
its extracted voltage.

A filter key must be valid **for that category**. Asking for `chemistry` under `category="motor"`
is not an empty result — it returns `error_code: "filter_not_applicable_to_category"` with the
keys that do apply. Categories with no attributes (for example `resistors` today) return
`filter_support: "none"` instead of silently returning zero rows.

### Request validation and diagnosis

Every empty/rejected result carries a single `diagnosis` object, so an agent branches on
`diagnosis.reason` instead of inferring intent from a warning string:

```json
{"ok": false, "error_code": "filter_not_applicable_to_category",
 "diagnosis": {"reason": "filter_not_applicable_to_category", "category": "motor",
               "not_applicable_keys": ["chemistry"],
               "applicable_keys": ["current_a", "rpm", "torque_kgcm", "voltage"],
               "filter_support": "partial"}}
```

Numeric keys reject non-numeric values (`rpm: "abc"` → `invalid_filter_value`) instead of
returning a confident zero. `tolerance` is a fraction of `eq` (`0.05` = ±5%) and must be
`0 < tolerance <= 1`; `tolerance: 5` is rejected. `price_max` must be `>= 0`.

### Fit filtering (will this part work for my need?)

| Key | Category | Unit | Examples |
|---|---|---|---|
| `voltage` | motor, battery | V | batteries: 3.7/7.4/11.1/14.8/22.2/51.2; motors: 3.3–48 (open enum) |
| `cells` / `capacity_mah` / `chemistry` | battery | S / mAh / enum | `3s`, `2200`, `lipo` |
| `discharge_c` | battery | C | `30` (30C) |
| `rpm` / `torque_kgcm` / `current_a` | motor | RPM / kg-cm / A | `300`, `4.2`, `2.5` (current is advisory) |
| `channels` | relay | count | `4` |

```python
# MCP / Python: "I need a 12 V motor, 1000 rpm or less, at least 5 kg-cm"
search_components(filters={"voltage": "12", "rpm": {"lte": 1000}, "torque_kgcm": {"gte": 5}},
                 category="motor")

# "Can I use this battery?" — 3.7 V LiPo, at least 1500 mAh
search_components(filters={"chemistry": "lipo", "voltage": "3.7",
                          "capacity_mah": {"gte": 1500}}, category="battery")
```

Per-product `specs_not_extracted` is scoped to keys that apply to that product's category, so a
battery is never reported as “missing rpm”; an unclassified product reports an empty list instead
of all nine catalog keys. The web equivalents are the same filters through `source=local`.

### Response contract (schema 2.0)

- List tools return `data.items` (with `items_are: "listings"` and `total_listings`); `results` /
  `listings` remain as aliases for older clients.
- Specifications use one shape: `specs` is `{key: value}` everywhere; `get_product` adds
  `spec_details` with value/type/confidence/evidence and keeps `extracted_specs` as an alias.
- `coverage[key]` has two explicit denominators: `catalog` and `page`. There is no longer a
  `result_present / result_of` pair computed from different populations.
- Filters report `filter_scope: "extracted_only"` and `data.filter_recall` with
  `base_matches`, `missing_spec` and `filtered_out_for_value`, plus a `filter_recall_gap`
  warning. The count is “products with extracted specs”, not “all products”.
- Price/stock freshness reports `status="live"` only when every returned candidate was refreshed;
  otherwise `partial`, with `refreshed_products`, `cached_products` and `verified_pct`.
- `search_by_mpn` rejects family tokens (`mpn_family_token`) and shared identifiers
  (`mpn_ambiguous`) unless `allow_ambiguous=True`; listings carry pack quantity, unit price and
  `price_is_quote`.

### MPN resolution

`search_by_mpn` ignores case, spaces, hyphens and underscores: `MPU6050`, `mpu-6050` and
`MPU 6050` resolve to the same listings. Listings include extracted `specs` and `price_source`,
pagination uses `next_cursor` (with `stale_cursor` protection), and a miss returns
`data.suggested_mpns` (closest catalog part numbers) instead of a dead end.

### Vendor descriptions and free-form specs

The taxonomy only defines attributes for a few categories. Vendor descriptions carry far more:
QuartzComponents and MakerBazar publish two-column `Specification | Value` tables (resistance,
tolerance, dielectric, package, dimensions, ...), and REES52/Robokits publish prose specs.

`scripts/backfill_descriptions.py` stores those descriptions in `product_descriptions` and parses
the tables/lists into `product_attributes`. The curated, filterable layer stays in
`product_specs`; `vendor_specs` is the larger free-form layer returned by `get_product`.

```powershell
# Shopify: quartzcomponents, makerbazar, rees52 (fast, ~75 catalog pages)
python scripts/backfill_descriptions.py --vendors quartzcomponents,makerbazar,rees52

# Robokits product pages (one request per product)
python scripts/backfill_descriptions.py --vendors robokits --limit 100

# Robu: ProductDetails GraphQL (JSON, no HTML/Cloudflare page fetch)
python scripts/backfill_descriptions.py --vendors robu --limit 50 --batch-size 25

# Robu fallback: product HTML (403s are skipped, resume from an allowed IP)
python scripts/backfill_descriptions.py --vendors robu --robu-source html --limit 50

# Inspect without writing
python scripts/backfill_descriptions.py --vendors quartzcomponents --limit 5 --dry-run
```

The script is idempotent (existing descriptions are skipped unless `--force`). `describe_catalog`
reports `products_with_descriptions`, `products_with_vendor_attributes` and
`vendor_attribute_rows`; `get_product` returns `vendor_specs` plus a real `description`
instead of falling back to the title.

**Robu without a rescrape.** Robu product pages are Cloudflare-protected, but its 85k titles are
already in SQLite. `scripts/backfill_title_attributes.py` extracts single-value specs from those
titles (`10K 1/4W`, `100uF 25V`, `3.7V 1300mAh`) into the same `product_attributes` layer, marked
`source=title_heuristic` and `confidence=0.6`. Kits/ranges are skipped, so the values are honest
but less rich than a vendor table.

```powershell
python scripts/backfill_title_attributes.py --vendors robu
python scripts/backfill_title_attributes.py --vendors robokits
```

Current catalog totals: **18,474 descriptions**, **261,085 vendor attribute rows**, and
**76,002 products with attributes** (Robu contributes 59k of those from titles alone).

**Robu GraphQL path.** Robu's Next.js frontend exposes
`POST https://robu.in/api/proxy/graphql/` with a `ProductDetails` operation. It returns
`description`, `short_description`, price and stock as JSON and is not blocked for us. The pipeline
uses aliased queries to fetch `--batch-size` products per request (default 10), with
`--update-price-stock` optionally writing the returned price/stock. This replaces 85k HTML page
fetches with a few thousand JSON calls.

```powershell
python scripts/backfill_descriptions.py --vendors robu --batch-size 25
```

**Completing the Robu catalog without the search API.** The sitemap registry still has pending
slugs after the 403-interrupted catalog run. `scripts/ingest_robu_graphql.py` fetches those slugs by
slug through ProductDetails, upserts the product, stores its description/attributes, and marks the
registry entry indexed. It is resumable and idempotent; rerunning continues with the next pending
slugs.

```powershell
python scripts/ingest_robu_graphql.py --limit 100 --batch-size 25
```

### Keeping the taxonomy current

Filterable attributes are defined in `config/taxonomy.yaml` and `config/extractors.yaml`. After
changing them, re-extract the whole catalog (idempotent):

```powershell
python scripts/backfill_specs.py
```

---

## 7. The Catalog Taxonomy

There is **one** taxonomy: `config/taxonomy.yaml`. It defines every category in
the catalog, the keywords that detect them, and the spec attributes that can be
filtered on for them. `src/spec/taxonomy_loader.py` loads it (validating every
referenced extractor and running their load-time tests), and
`src/spec/taxonomy_loader.py::classify_product()` is the single classifier.

### The problem it solves

Every vendor files its products under its own shelf names, so the raw
`products.category` column holds **3,181 different strings** for the same
catalog ("Film Resistors", "MLCCs- Multi Layer Ceramic Capacitors", "Popular
Products", "New Product 4", …). Those strings cannot be filtered on, so they
are mapped onto one shared taxonomy of **32 categories** stored in
`products.category_canonical`. The mapping is recorded in `category_map`, so it
stays auditable.

### How a product is classified

1. The vendor's own category string is matched against the rules. This is the
   strongest signal because it is what the vendor filed the part under.
2. The product title is used when the vendor string carries no signal
   ("New Products 5", "Popular Products", ...).
3. `products.spec_category` is the last resort.
4. Anything left over lands in `Other Components` (about 1.6% of the catalog).

Matching is token based and plural tolerant, so `resistor` matches `Resistors`
and `switch` matches `Switches` without every plural having to be listed by
hand. When several categories match, the most specific one wins — the longest
matching keyword — so `USB Interface Module` is a module and not a connector,
and `Battery Charger Module` is a battery and not a module.

### Spec attributes (the "will it fit?" filters)

A category may declare `attributes`: the specs that can be filtered on for it.
The three spec categories carry them:

| Category | Filterable attributes |
|---|---|
| `motor` | `voltage`, `rpm`, `current_a`, `torque_kgcm` |
| `battery` | `voltage`, `cells`, `capacity_mah`, `discharge_c`, `chemistry` |
| `relay` | `channels` |

The values are extracted from the product title by the regex extractors in
`config/extractors.yaml` and stored in `product_specs`. **The same filters are
available in the web UI** (the sidebar grows those controls when you pick one
of the three categories), in the JSON API (`spec_filters`) and in the MCP tools
(`filters=`) — they all go through `src/spec/filters.py`, so a filter means
exactly the same thing everywhere.

### Re-applying the taxonomy

```powershell
# Classify everything that does not have a canonical category yet
python scripts/backfill_categories.py

# Re-classify the whole catalog after editing the rules
python scripts/backfill_categories.py --force

# Just print the current distribution
python scripts/backfill_categories.py --report
```

Re-running the spec extraction (after changing `config/extractors.yaml` or the
attributes in `config/taxonomy.yaml`) is a separate, idempotent step:

```powershell
python scripts/backfill_specs.py
```

### Using the categories

- Web UI: the category rail under the search box, the sidebar facet list, and
  `/?category=<slug>` for browsing. Categories with attributes also show their
  spec filters.
- JSON API: `GET /api/categories` (taxonomy + live counts + filterable
  attributes), and `GET /api/search?...&category=<slug>&spec_filters=<json>`.
- MCP: `search_components(category="resistors")` now works for all 32
  categories; `describe_catalog` returns the full taxonomy with the filterable
  attributes of each category.

---
## 8. Giving an Agent Access (MCP)

The MCP server lives in `mcp_server/server.py` and exposes **8 tools**:

| Tool | What it does |
|---|---|
| `describe_catalog` | Catalog size, vendor/category distribution, the full taxonomy with filterable attributes, data freshness |
| `describe_spec` | Value distribution, range and coverage for one spec attribute |
| `list_spec_keys` | Every queryable spec attribute with fill counts |
| `count_components` | Size a query without fetching rows + filter coverage |
| `search_components` | Search by text and/or spec filters, side-by-side vendor offers, cursor pagination |
| `search_by_mpn` | Resolve a part number (or vendor SKU) to its listings, with typo suggestions |
| `get_product` | Full record for one product: specs, offers, confidence |
| `health` | Liveness, dependency versions, database path |

It speaks **stdio** (for local clients) and **SSE** (for remote ones).

### 1. Register it with your client

```powershell
python scripts/setup_mcp.py show               # print the config, change nothing
python scripts/setup_mcp.py claude-code        # Claude Code  -> .mcp.json in the repo
python scripts/setup_mcp.py claude-desktop     # Claude Desktop -> %APPDATA%\Claude\claude_desktop_config.json
python scripts/setup_mcp.py cursor             # Cursor        -> .cursor/mcp.json
python scripts/setup_mcp.py vscode             # VS Code       -> .vscode/mcp.json
python scripts/setup_mcp.py windsurf           # Windsurf      -> ~/.codeium/windsurf/mcp_config.json
```

Existing config files are merged, never overwritten. Add `--dry-run` to see what
would change first, and `--scope user|project|local` to pick a Claude Code scope.

What gets written (the same for every client):

```json
{
  "mcpServers": {
    "aggregate": {
      "command": "<project>\.venv\Scripts\python.exe",
      "args": ["<project>\scripts\run_mcp.py"],
      "cwd": "<project>"
    }
  }
}
```

`scripts/run_mcp.py` is a thin launcher: it pins the project root as the
working directory, because the server imports `db`, `config` and `models` from
there and some MCP clients do not send a working directory.

### 2. Approve it (Claude Code only)

Project-scoped servers are trusted on first use. Claude Code shows a prompt the
first time it starts in a folder that contains a `.mcp.json`:

```powershell
claude          # approve "aggregate" when prompted
claude mcp list # should show "aggregate: ... ✓ connected"
```

### 3. Check it works

```powershell
python scripts/run_mcp.py --list-tools   # the 8 tools, without a client
claude mcp get aggregate                  # what the client will run
```

The `health` tool reports the database path and integrity, so you can confirm
the agent is talking to the same catalog you are.

### 4. Using it from an agent

Once connected, the agent can discover the catalog first and then query it:

```
describe_catalog()                                  # taxonomy, counts, filterable attributes
search_components(category="motor", filters={"voltage": {"gte": 12}, "rpm": {"lte": 1000}})
search_components(query="0805", category="resistors")
count_components(category="battery", filters={"chemistry": {"eq": "lipo"}})
search_by_mpn(mpn="MPU6050")
```

The `category` argument accepts all 32 canonical categories; the three that
carry attributes (`motor`, `battery`, `relay`) also accept `filters=`.

### Remote clients (SSE)

```powershell
python scripts/run_mcp.py --transport sse --host 0.0.0.0 --port 8001
```

Then point the client at `http://<host>:8001/sse`. Bind to `0.0.0.0` only on a
network you trust - the server has no authentication of its own.

### Removing it

```powershell
claude mcp remove aggregate -s project
```

or delete the `aggregate` entry from the client's config file.

---
