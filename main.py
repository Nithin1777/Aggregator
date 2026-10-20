"""
Indian Component Aggregator Engine - Main Entry Point (CLI & FastAPI Web Application).
"""
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Ensure UTF-8 output on Windows terminals and pipes
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
import typer

from config import settings
import db
from models import ClickFeedback, SearchResponse
from search_service import AggregatorEngine
from src.search.query_terms import parse_price_constraints
from src.web.render import icon_svg

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("aggregator")

# Console for Rich CLI outputs
console = Console()

# ---------------------------------------------------------------------------
# FastAPI Application & Web Layer
# ---------------------------------------------------------------------------
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    db.sync_category_taxonomy()
    get_engine()
    yield
    global engine_instance
    if engine_instance:
        await engine_instance.close()

web_app = FastAPI(
    title="Aggregate - Component Search",
    description="Multi-vendor electronic component price and stock comparison engine (Robu, QuartzComponents, MakerBazar, Robokits, REES52)",
    version="2.0.0",
    lifespan=lifespan
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _static_asset_version() -> str:
    """Cache-busting token for JS/CSS so a fix is never hidden by a stale tab."""
    try:
        files = [
            STATIC_DIR / "js" / "aggregate.js",
            STATIC_DIR / "css" / "aggregate.css",
        ]
        return str(max(int(path.stat().st_mtime) for path in files if path.exists()))
    except Exception:  # noqa: BLE001 - never break page rendering for a cache key
        return "1"


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["icon"] = icon_svg
templates.env.globals["asset_version"] = _static_asset_version()

web_app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Global engine instance for FastAPI
engine_instance: Optional[AggregatorEngine] = None

# Strong references to fire-and-forget ingest tasks. Without this, asyncio may
# garbage-collect a running task mid-flight and a failure is only logged as
# "Task exception was never retrieved".
_background_tasks: set = set()


def _track_background_task(task: "asyncio.Task") -> None:
    """Keeps a strong reference and surfaces task failures in the log."""
    _background_tasks.add(task)

    def _on_done(t: "asyncio.Task") -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            logger.warning("[Ingest Task] cancelled before completion")
            return
        exc = t.exception()
        if exc is not None:
            logger.error("[Ingest Task] failed: %s", exc)

    task.add_done_callback(_on_done)

def get_engine() -> AggregatorEngine:
    global engine_instance
    if engine_instance is None:
        engine_instance = AggregatorEngine()
    return engine_instance


def _normalize_refresh_mode(value) -> str:
    """Normalizes the refresh mode (direct calls skip FastAPI validation)."""
    if isinstance(value, str) and value in ("auto", "force", "off"):
        return value
    return "auto"


def _normalize_optional(value) -> Optional[str]:
    """Normalizes an optional query parameter (direct calls skip validation)."""
    if isinstance(value, str):
        return value.strip() or None
    return None


def _normalize_flag(value) -> bool:
    """Normalizes a boolean query parameter (direct calls skip validation)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def _normalize_limit(value, default: int = 70) -> int:
    """Normalizes the result limit (direct calls skip FastAPI validation)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(1, min(200, value))


def _normalize_page(value) -> int:
    """Normalizes the 1-based page number (direct calls skip validation)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 1
    return max(1, value)


def _normalize_page_size(value, fallback_limit=None) -> int:
    """Normalizes the cards-per-page size (direct calls skip validation)."""
    default = settings.search_page_size
    raw = value if isinstance(value, int) and not isinstance(value, bool) else None
    if raw is None and isinstance(fallback_limit, int) and not isinstance(fallback_limit, bool):
        raw = fallback_limit
    if raw is None:
        raw = default
    return max(1, min(int(settings.search_page_size_max), int(raw)))


def _normalize_price(value) -> Optional[float]:
    """Normalizes an optional price bound (direct calls skip validation)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _normalize_spec_filters(value) -> Dict[str, Any]:
    """Parses the `spec_filters` query parameter (a JSON object)."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# Seed terms shown on the landing page. They are replaced by the live search
# log once the catalog has seen enough traffic.
_DEFAULT_POPULAR_SEARCHES = [
    "esp32", "arduino", "dht11", "resistor", "lm2596", "servo", "bme280",
]


def _popular_searches(limit: int = 8) -> list:
    """Returns popular queries, topped up with the seeded defaults."""
    try:
        popular = db.get_popular_queries(limit=limit, min_count=2)
    except Exception:  # noqa: BLE001 - the landing page must never fail
        popular = []

    merged = list(popular)
    for term in _DEFAULT_POPULAR_SEARCHES:
        if len(merged) >= limit:
            break
        if term not in merged:
            merged.append(term)
    return merged[:limit]


def _page_context(
    query: str = "",
    category: str = "",
    vendor: str = "",
    in_stock: bool = False,
    page: int = 1,
) -> dict:
    """Shared template context for the public pages."""
    categories = db.get_categories()
    vendors = db.get_vendor_counts()
    stats = db.get_db_stats()
    return {
        "query": query,
        "category": category,
        "vendor": vendor,
        "in_stock": in_stock,
        "page": page,
        "has_results": bool(query or category or vendor),
        "categories": categories,
        "vendors": vendors,
        "popular_searches": _popular_searches(),
        "catalog_stats": {
            "total_products": stats.get("total_products", 0),
            "in_stock_products": stats.get("in_stock_products", 0),
            "vendor_count": len(stats.get("vendors", {})) or len(vendors),
            "category_count": len([c for c in categories if c["count"] > 0]),
        },
    }


@web_app.get("/", response_class=HTMLResponse)
async def web_ui(
    request: Request,
    q: Optional[str] = Query(None, description="Search query string"),
    category: Optional[str] = Query(None, description="Canonical category slug"),
    vendor: Optional[str] = Query(None, description="Vendor slug"),
    in_stock: bool = Query(False, description="Only show listings that are in stock"),
    page: int = Query(1, ge=1, description="Results page"),
):
    """Search page: type-ahead suggestions, category filters and comparison cards."""
    context = _page_context(
        query=(q.strip() if isinstance(q, str) else ""),
        category=_normalize_optional(category) or "",
        vendor=_normalize_optional(vendor) or "",
        in_stock=_normalize_flag(in_stock),
        page=_normalize_page(page),
    )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context=context,
    )


@web_app.get("/categories", response_class=HTMLResponse)
async def web_categories(request: Request):
    """Browse page listing every canonical category with live counts."""
    context = _page_context()
    return templates.TemplateResponse(
        request=request,
        name="categories.html",
        context={**context, "active_nav": "categories"},
    )


@web_app.get("/vendors", response_class=HTMLResponse)
async def web_vendors(request: Request):
    """Browse page listing the compared vendors and their catalog size."""
    context = _page_context()
    return templates.TemplateResponse(
        request=request,
        name="vendors.html",
        context={**context, "active_nav": "vendors"},
    )


@web_app.get("/api/categories")
async def api_categories(
    vendor: Optional[str] = Query(None, description="Scope counts to one vendor"),
):
    """Returns the canonical category taxonomy with live product counts."""
    categories = db.get_categories(vendor=_normalize_optional(vendor))
    return {
        "categories": categories,
        "total": sum(c["count"] for c in categories),
    }


@web_app.get("/api/suggest")
async def api_suggest(
    q: str = Query(..., min_length=1, description="Partially typed query"),
    limit: int = Query(8, ge=1, le=20, description="Max suggestions"),
    category: Optional[str] = Query(None, description="Restrict to one category"),
):
    """Type-ahead suggestions: part numbers, products and categories."""
    prefix = q.strip() if isinstance(q, str) else ""
    if not prefix:
        return {"query": prefix, "suggestions": [], "corrections": []}
    return {
        "query": prefix,
        "suggestions": db.suggest_products(
            prefix, limit=_normalize_limit(limit, 8), category=_normalize_optional(category)
        ),
        "corrections": db.suggest_mpns(prefix, limit=3),
    }


@web_app.get("/api/popular")
async def api_popular(limit: int = Query(8, ge=1, le=25)):
    """Returns the most searched queries so far."""
    return {"queries": _popular_searches(limit=_normalize_limit(limit, 8))}


@web_app.get("/search", response_model=SearchResponse)
@web_app.get("/api/search", response_model=SearchResponse)
async def api_search(
    q: str = Query("", description="Component name or part number"),
    source: str = Query("local", pattern="^(live|local)$", description="Search source"),
    limit: Optional[int] = Query(
        None, ge=1, le=200,
        description="Legacy alias for page_size (kept for older clients)",
    ),
    page: int = Query(1, ge=1, description="1-based page of comparison cards"),
    page_size: Optional[int] = Query(
        None, ge=1, le=50, description="Comparison cards per page (default 20)"
    ),
    refresh: str = Query(
        "auto",
        pattern="^(auto|force|off)$",
        description="Live price/stock refresh: 'auto' (TTL/background), 'force', or 'off' (cache only)"
    ),
    category: Optional[str] = Query(None, description="Canonical category slug"),
    vendor: Optional[str] = Query(None, description="Vendor slug"),
    in_stock: bool = Query(False, description="Only return listings that are in stock"),
    price_min: Optional[float] = Query(None, ge=0, description="Minimum price in INR"),
    price_max: Optional[float] = Query(None, ge=0, description="Maximum price in INR"),
    spec_filters: Optional[str] = Query(
        None,
        description='JSON object of spec filters, e.g. {"voltage": {"gte": 12}}',
    ),
):
    """
    JSON API endpoint for real-time or local component search.

    Results are paginated at the comparison-card level: `page`/`page_size` select
    which ranked groups are returned, so irrelevant long-tail matches stay on
    later pages instead of bloating the first response.

    Without a query string the endpoint falls back to browsing the filtered
    category, so `/?category=resistors` returns real listings.
    """
    engine = get_engine()
    query = (q or "").strip() if isinstance(q, str) else ""
    original_query = query
    # "resistor under ₹500" / "motor 300 to 1200" carry a budget in the text.
    query, text_price_min, text_price_max = parse_price_constraints(query)
    category = _normalize_optional(category)
    vendor = _normalize_optional(vendor)
    in_stock = _normalize_flag(in_stock)
    page = _normalize_page(page)
    page_size = _normalize_page_size(page_size, fallback_limit=limit)
    price_min = _normalize_price(price_min)
    price_max = _normalize_price(price_max)
    if price_min is None:
        price_min = text_price_min
    if price_max is None:
        price_max = text_price_max
    if price_min is not None and price_max is not None and price_min > price_max:
        price_min, price_max = price_max, price_min
    spec_filters = _normalize_spec_filters(spec_filters)

    if spec_filters:
        # Validate up front so a bad filter is a clean 400, not a 500.
        try:
            with db.get_db_connection() as conn:
                advertised = db.load_catalog_specs(conn)
            normalized, error = db.parse_and_validate_filters(spec_filters, advertised)
        except Exception:  # noqa: BLE001 - validation must not break the request
            normalized, error = None, None
        if error:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": error["error_code"],
                    "message": error["message"],
                    "valid_keys": error.get("valid_keys"),
                    "suggestions": error.get("suggestions"),
                },
            )
        if normalized is not None:
            spec_filters = normalized

    if not query and (
        category or vendor or spec_filters
        or price_min is not None or price_max is not None
    ):
        # Browse mode: list the filtered catalog instead of searching.
        resp = await engine.browse_local(
            page=page,
            page_size=page_size,
            category=category or None,
            vendor=vendor or None,
            in_stock_only=bool(in_stock),
            spec_filters=spec_filters or None,
            price_min=price_min,
            price_max=price_max,
        )
    else:
        if not query:
            return SearchResponse(query=query, total_results=0, source="local_db")
        if source == "local":
            resp = await engine.search_local_refreshed(
                query,
                page=page,
                page_size=page_size,
                vendor=vendor or None,
                in_stock_only=bool(in_stock),
                refresh_mode=_normalize_refresh_mode(refresh),
                category=category or None,
                spec_filters=spec_filters or None,
                price_min=price_min,
                price_max=price_max,
                background_refresh=True,
            )
        else:
            resp = await engine.search_live(
                query=query, limit_per_vendor=min(page_size, 25)
            )

        try:
            db.record_search_query(original_query or query)
        except Exception:  # noqa: BLE001 - telemetry must never break a search
            pass

    for g in resp.comparison_groups:
        seen = []
        for o in g.offers:
            t = (o.title or "").strip()
            if t and t not in seen:
                seen.append(t)
        g.distinct_titles = seen

    return resp

@web_app.post("/feedback")
async def api_feedback(feedback: ClickFeedback):
    """Record user click telemetry for conversion and ranking audit."""
    click_id = db.record_click(
        query=feedback.query,
        group_id=feedback.group_id,
        vendor=feedback.vendor,
        product_id=feedback.product_id
    )
    return {"status": "ok", "id": click_id}

@web_app.get("/api/status")
async def api_status():
    """Returns database statistics, product counts per vendor, and scrape checkpoints."""
    return db.get_db_stats()


@web_app.get("/health")
async def api_health():
    """Lightweight liveness probe used by process supervisors."""
    return {"status": "ok", "service": "aggregate"}

@web_app.post("/api/ingest")
async def api_ingest(
    vendor: str = Query(..., description="Vendor to ingest: 'quartzcomponents', 'makerbazar', 'robu', 'robokits', or 'rees52'"),
    max_pages: Optional[int] = Query(None, description="Max pages to scrape"),
    resume: bool = Query(True, description="Resume from last checkpoint")
):
    """Triggers batch catalog ingestion asynchronously."""
    engine = get_engine()
    if vendor not in engine.adapters:
        return {"error": f"Invalid vendor '{vendor}'. Allowed: {list(engine.adapters.keys())}"}

    async def _run_task():
        async for progress in engine.run_vendor_ingest(vendor_id=vendor, max_pages=max_pages, resume=resume):
            logger.info(f"[Ingest Task] {progress.vendor}: page {progress.current_page} - {progress.message}")

    _track_background_task(asyncio.create_task(_run_task()))
    return {
        "status": "started",
        "vendor": vendor,
        "max_pages": max_pages,
        "resuming": resume
    }


# ---------------------------------------------------------------------------
# CLI Application (Typer & Rich)
# ---------------------------------------------------------------------------
cli = typer.Typer(
    help="Aggregate - search and compare components across Indian vendors",
    no_args_is_help=True
)

@cli.command("search")
def cli_search(
    query: str = typer.Argument(..., help="Search query (e.g. 'esp32', 'mpu6050', 'lm2596')"),
    live: bool = typer.Option(True, "--live/--local", help="Query live vendor endpoints vs local SQLite index"),
    limit: int = typer.Option(15, "--limit", "-l", help="Max results per vendor"),
    refresh: bool = typer.Option(
        True, "--refresh/--cache-only",
        help="Local mode: verify price/stock live before display (falls back to cached values offline)"
    ),
):
    """Search and compare electronic components across Robu, QuartzComponents, and MakerBazar."""
    async def _search():
        engine = AggregatorEngine()
        try:
            with console.status(f"[bold green]Searching for '{query}' ({'Live APIs' if live else 'Local DB'}): refreshing price/stock...[/bold green]"):
                if live:
                    resp = await engine.search_live(query=query, limit_per_vendor=limit)
                else:
                    resp = await engine.search_local_refreshed(
                        query=query,
                        limit=limit * 3,
                        refresh_mode="auto" if refresh else "off"
                    )

            if not resp.comparison_groups:
                console.print(f"[yellow]No components found for '{query}'.[/yellow]")
                return

            console.print(
                Panel.fit(
                    f"[bold white]Found {resp.total_results} matching items across {len(resp.comparison_groups)} component groups[/bold white]\n"
                    f"[dim]Source: {resp.source.upper()}[/dim]",
                    title="Component Search Summary",
                    border_style="blue"
                )
            )

            if resp.price_refresh:
                status_style = {
                    "live": "bold green",
                    "partial": "yellow",
                    "fresh_cache": "cyan",
                    "cached": "yellow",
                    "offline_cooldown": "yellow",
                }.get(resp.price_refresh.status, "dim")
                console.print(
                    f"[{status_style}]Price/Stock [{resp.price_refresh.status}]:[/{status_style}] {resp.price_refresh.message}"
                )

            for group in resp.comparison_groups:
                title_line = f"[bold cyan]{group.canonical_name}[/bold cyan]"
                if group.mpn:
                    title_line += f"  [bold magenta]MPN: {group.mpn}[/bold magenta]"

                best_badge = ""
                if group.best_price:
                    best_badge = f" [bold green]Best: Rs. {group.best_price:.2f} ({group.best_vendor})[/bold green]"

                console.print(f"\n{title_line}{best_badge}")

                table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
                table.add_column("Vendor", style="bold")
                table.add_column("Stock Status")
                table.add_column("Price (INR)", justify="right")
                table.add_column("Product Link / Title", style="dim")

                for offer in group.offers:
                    vendor_style = {
                        "robu": "blue",
                        "quartzcomponents": "magenta",
                        "makerbazar": "yellow",
                        "robokits": "cyan",
                        "rees52": "green"
                    }.get(offer.vendor, "white")

                    stock_str = "[green]In Stock[/green]" if offer.available else "[red]Out of Stock[/red]"
                    price_str = f"Rs. {offer.price:.2f}"
                    if group.best_price and offer.price == group.best_price and offer.available:
                        price_str = f"[bold green]* Rs. {offer.price:.2f} (Lowest)[/bold green]"

                    table.add_row(
                        f"[{vendor_style}]{offer.vendor_name}[/{vendor_style}]",
                        stock_str,
                        price_str,
                        f"[link={offer.product_url}]{offer.title[:55]}[/link]"
                    )

                console.print(table)

        finally:
            await engine.close()

    asyncio.run(_search())

@cli.command("ingest")
def cli_ingest(
    vendor: Optional[str] = typer.Argument(
        None,
        help="Vendor ID: 'quartzcomponents', 'makerbazar', 'robu', or omit to ingest all sequentially"
    ),
    max_pages: Optional[int] = typer.Option(
        None,
        "--max-pages", "-m",
        help="Max number of pages to fetch in this session (e.g. 2 for testing, or omit to run continuous)"
    ),
    resume: bool = typer.Option(
        True,
        "--resume/--fresh",
        help="Resume from last saved checkpoint or start fresh from page 1"
    )
):
    """
    Runs continuable batch catalog ingestion.
    Safely checkpoints progress into SQLite so you can stop anytime and resume later.
    """
    async def _ingest():
        engine = AggregatorEngine()
        vendors_to_run = [vendor] if vendor else list(engine.adapters.keys())

        try:
            for v_id in vendors_to_run:
                adapter = engine.adapters.get(v_id)
                if not adapter:
                    console.print(f"[bold red]Unknown vendor '{v_id}'. Supported: {list(engine.adapters.keys())}[/bold red]")
                    continue

                checkpoint = db.get_checkpoint(v_id)
                start_page = checkpoint.last_page + 1 if (resume and checkpoint.last_page > 0 and checkpoint.status != "completed") else 1

                console.print(Panel.fit(
                    f"[bold]Vendor:[/bold] {adapter.vendor_name} ({v_id})\n"
                    f"[bold]Starting Page:[/bold] {start_page} (Resume: {resume})\n"
                    f"[bold]Session Limit:[/bold] {max_pages if max_pages is not None else 'Continuous / All'}\n"
                    f"[dim]Data is saved and checkpointed to SQLite after every page.[/dim]",
                    title="Catalog Ingestion Session",
                    border_style="blue"
                ))

                with Progress(
                    TextColumn("[bold cyan]>[/bold cyan]"),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TextColumn("[bold cyan]{task.fields[info]}[/bold cyan]"),
                    TimeElapsedColumn(),
                    console=console
                ) as progress:
                    task = progress.add_task(
                        f"Ingesting {adapter.vendor_name}...",
                        total=max_pages,
                        info="Starting..."
                    )

                    async for step in engine.run_vendor_ingest(vendor_id=v_id, max_pages=max_pages, resume=resume):
                        progress.update(
                            task,
                            advance=1,
                            info=f"Page {step.current_page} (+{step.items_added} items, Total: {step.total_vendor_items})"
                        )

                console.print(f"[bold green]Session complete for {adapter.vendor_name}. Checkpoint saved.[/bold green]\n")

        finally:
            await engine.close()

    asyncio.run(_ingest())

@cli.command("status")
def cli_status():
    """Display local database statistics and continuable scraping checkpoints."""
    db.init_db()
    stats = db.get_db_stats()
    ps = stats.get("price_stock", {})

    console.print(Panel.fit(
        f"[bold]Total Indexed Products:[/bold] {stats['total_products']:,}\n"
        f"[bold]Total In-Stock Items:[/bold] {stats['in_stock_products']:,}\n"
        f"[bold]Price/Stock Live-Verified:[/bold] {ps.get('with_live_price_stock', 0):,} "
        f"(cached fallback only: {ps.get('with_cached_price_stock_only', 0):,})\n"
        f"[bold]Last Price/Stock Refresh:[/bold] {str(ps.get('last_successful_refresh') or 'Never')[:19]}\n"
        f"[bold]Database Location:[/bold] {settings.db_path}",
        title="Aggregate Database Status",
        border_style="green"
    ))

    # Vendor breakdown table
    table = Table(title="Vendor Breakdown & Checkpoints", show_header=True, header_style="bold cyan")
    table.add_column("Vendor Slug")
    table.add_column("Total In DB", justify="right")
    table.add_column("In Stock", justify="right")
    table.add_column("Last Checkpoint Page", justify="right")
    table.add_column("Scrape Status")
    table.add_column("Last Updated")

    all_vendor_ids = ["robu", "quartzcomponents", "makerbazar", "robokits", "rees52"]
    for vid in all_vendor_ids:
        vdata = stats["vendors"].get(vid, {"total": 0, "in_stock": 0})
        chk = stats["checkpoints"].get(vid, {"last_page": 0, "status": "idle", "updated_at": "Never"})

        status_style = {
            "completed": "bold green",
            "in_progress": "bold yellow",
            "paused": "cyan",
            "error": "bold red",
            "idle": "dim"
        }.get(chk["status"], "white")

        table.add_row(
            vid,
            f"{vdata['total']:,}",
            f"{vdata['in_stock']:,}",
            str(chk["last_page"]),
            f"[{status_style}]{chk['status']}[/{status_style}]",
            str(chk["updated_at"] or "Never")[:19]
        )

    console.print(table)

    # Robu Ground-Truth Parity Tracker
    reg = stats.get("robu_registry", {})
    if reg.get("total_registry", 0) > 0:
        cov = reg.get("coverage_pct", 0.0)
        cov_color = "green" if cov >= 99 else ("yellow" if cov >= 50 else "cyan")
        console.print(
            Panel.fit(
                f"[bold]Ground-Truth Sitemaps Registry:[/bold] {reg['total_registry']:,} URLs\n"
                f"[bold]Fully Indexed in Database:[/bold] {reg['indexed']:,} products\n"
                f"[bold]Remaining Pending Ingestion:[/bold] {reg['pending']:,} products\n"
                f"[bold]Robu Catalog Coverage:[/bold] [{cov_color}]{cov}% Complete[/{cov_color}]",
                title="Robu.in 100% Catalog Parity Registry",
                border_style="magenta"
            )
        )

@cli.command("categories")
def cli_categories(
    vendor: Optional[str] = typer.Option(None, "--vendor", "-v", help="Scope counts to one vendor"),
):
    """Show the canonical catalog taxonomy with live part counts."""
    db.init_db()
    categories = db.get_categories(vendor=vendor)
    total = sum(c["count"] for c in categories)

    table = Table(title="Canonical categories", show_header=True, header_style="bold cyan")
    table.add_column("Slug")
    table.add_column("Label")
    table.add_column("Parts", justify="right")
    table.add_column("In stock", justify="right")
    table.add_column("Share", justify="right")

    for cat in categories:
        share = (cat["count"] * 100.0 / total) if total else 0.0
        table.add_row(
            cat["slug"],
            cat["label"],
            f"{cat['count']:,}",
            f"{cat['in_stock']:,}",
            f"{share:.1f}%",
        )

    console.print(table)
    console.print(
        f"[dim]{total:,} products classified into {len(categories)} categories. "
        f"Re-apply the taxonomy with: python scripts/backfill_categories.py --force[/dim]"
    )


@cli.command("robu-sync")
def cli_robu_sync(
    sitemaps: int = typer.Option(22, "--sitemaps", "-s", help="Number of sitemaps to harvest (1..22)")
):
    """Synchronize Robu ground-truth product registry from its 22 sitemaps (~22,000 URLs)."""
    async def _sync():
        engine = AggregatorEngine()
        try:
            robu_adapter = engine.adapters.get("robu")
            with console.status("[bold green]Harvesting Robu XML sitemaps to build 100% ground-truth registry...[/bold green]"):
                added = await robu_adapter.sync_sitemaps_to_registry(max_sitemaps=sitemaps)
            stats = db.get_robu_registry_stats()
            console.print(f"[bold green]Registry sync complete! Total registered products: {stats['total_registry']:,} (+{added} new).[/bold green]")
        finally:
            await engine.close()

    asyncio.run(_sync())

@cli.command("gray-zone")
def cli_gray_zone(
    limit: int = typer.Option(30, "--limit", "-n", help="Number of gray-zone records to display"),
    high_risk: bool = typer.Option(False, "--high-risk", help="Filter to show only high-risk anomalies")
):
    """Inspect the live gray-zone matching telemetry log and anomaly flags."""
    from gray_zone import print_gray_zone_table
    db.init_db()
    print_gray_zone_table(limit=limit, high_risk_only=high_risk)

@cli.command("serve")
def cli_serve(
    host: str = typer.Option(settings.host, "--host", "-h", help="Bind host"),
    port: int = typer.Option(settings.port, "--port", "-p", help="Bind port"),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload for development")
):
    """Start the FastAPI Web UI and REST API server."""
    import uvicorn
    console.print(f"[bold green]Starting Web UI on http://{host}:{port}[/bold green]")
    uvicorn.run("main:web_app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    cli()
