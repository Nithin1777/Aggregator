"""
Indian Component Aggregator Engine - Main Entry Point (CLI & FastAPI Web Application).
"""
import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

# Ensure UTF-8 output on Windows terminals and pipes
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from fastapi import FastAPI, Query, Request
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
    get_engine()
    yield
    global engine_instance
    if engine_instance:
        await engine_instance.close()

web_app = FastAPI(
    title="Octopart India - Component Aggregator Engine",
    description="Multi-vendor electronic component price and stock comparison engine (Robu, QuartzComponents, MakerBazar)",
    version="1.0.0",
    lifespan=lifespan
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

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

@web_app.get("/", response_class=HTMLResponse)
async def web_ui(
    request: Request,
    q: Optional[str] = Query(None, description="Search query string"),
    source: str = Query("local", description="Search source: 'live' or 'local'")
):
    """Web interface search page with comparison groups."""
    engine = get_engine()
    results = []
    comparison_groups = []
    total_results = 0

    if q and q.strip():
        if source == "local":
            # search_local is synchronous and CPU-bound; keep it off the event loop.
            resp = await asyncio.to_thread(engine.search_local, q.strip())
        else:
            resp = await engine.search_live(query=q.strip())

        results = resp.results
        comparison_groups = resp.comparison_groups
        total_results = resp.total_results

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "query": q or "",
            "source": source,
            "results": results,
            "comparison_groups": comparison_groups,
            "total_results": total_results
        }
    )

@web_app.get("/search", response_model=SearchResponse)
@web_app.get("/api/search", response_model=SearchResponse)
async def api_search(
    q: str = Query(..., min_length=1, description="Component name or part number"),
    source: str = Query("local", pattern="^(live|local)$", description="Search source"),
    limit: int = Query(70, ge=1, le=200, description="Max results limit")
):
    """JSON API endpoint for real-time or local component search."""
    engine = get_engine()
    if source == "local":
        resp = await asyncio.to_thread(engine.search_local, q, limit)
    else:
        resp = await engine.search_live(query=q, limit_per_vendor=min(limit // 2, 25))

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
    help="Indian Component Aggregator Engine ('Octopart for India')",
    no_args_is_help=True
)

@cli.command("search")
def cli_search(
    query: str = typer.Argument(..., help="Search query (e.g. 'esp32', 'mpu6050', 'lm2596')"),
    live: bool = typer.Option(True, "--live/--local", help="Query live vendor endpoints vs local SQLite index"),
    limit: int = typer.Option(15, "--limit", "-l", help="Max results per vendor"),
):
    """Search and compare electronic components across Robu, QuartzComponents, and MakerBazar."""
    async def _search():
        engine = AggregatorEngine()
        try:
            with console.status(f"[bold green]Searching for '{query}' ({'Live APIs' if live else 'Local DB'})...[/bold green]"):
                if live:
                    resp = await engine.search_live(query=query, limit_per_vendor=limit)
                else:
                    resp = engine.search_local(query=query, limit=limit * 3)

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

    console.print(Panel.fit(
        f"[bold]Total Indexed Products:[/bold] {stats['total_products']:,}\n"
        f"[bold]Total In-Stock Items:[/bold] {stats['in_stock_products']:,}\n"
        f"[bold]Database Location:[/bold] {settings.db_path}",
        title="Octopart India Database Status",
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
