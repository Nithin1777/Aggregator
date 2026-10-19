# Octopart for India (Indian Component Aggregator Engine)

A high-performance component aggregation search engine that indexes, queries, and compares electronic component pricing and live stock availability across three primary Indian vendors:
1. **Robu.in**
2. **QuartzComponents**
3. **MakerBazar**

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
├── search_service.py     # Parallel federation, comparison clustering, and continuable ingestion
├── main.py               # Typer CLI dashboard and FastAPI Web Application
├── templates/
│   └── index.html        # Modern responsive search UI
├── tests/                # Comprehensive test suite (12 unit and integration tests)
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
python -m pytest -v
```

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

Launch the web application:
```powershell
python main.py serve --port 8000
```
Open your browser at **http://localhost:8000**:
- Clean Octopart-style interface.
- Real-time side-by-side comparison tables.
- Live stock badges (`In Stock` / `Out of Stock`).
- Lowest price highlight badge.
- Instant toggle between Live API and Local FTS Database.

### JSON API Endpoints:
- `GET /api/search?q={query}&source=live` — Multi-vendor search JSON with comparison groups.
- `GET /api/search?q={query}&source=local` — Sub-millisecond local indexed search JSON.
- `GET /api/status` — Database item counts, in-stock counts, and scrape checkpoint statuses.
- `POST /api/ingest?vendor={vendor}&max_pages={n}&resume=true` — Trigger background catalog ingestion.
