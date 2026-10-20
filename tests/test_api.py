"""
Test suite for FastAPI endpoints and engine query layer.
"""
import pytest
from main import api_search, api_status


@pytest.mark.asyncio
async def test_api_status_endpoint():
    data = await api_status()
    assert "total_products" in data
    assert "vendors" in data
    assert "checkpoints" in data
    assert "robu" in data["vendors"]
    assert "quartzcomponents" in data["vendors"]
    assert "makerbazar" in data["vendors"]

@pytest.mark.asyncio
async def test_api_search_endpoint_local():
    data = await api_search(q="esp32", source="local", limit=10)
    assert data.query == "esp32"
    assert data.source == "local_db"
    assert isinstance(data.comparison_groups, list)

@pytest.mark.asyncio
async def test_web_ui_endpoint():
    from starlette.requests import Request
    from main import web_app, web_ui
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "app": web_app,
        "router": web_app.router,
        "root_path": "",
        "scheme": "http",
        "server": ("testserver", 80),
        "query_string": b"",
    }
    req = Request(scope)
    resp = await web_ui(request=req, q="esp32")
    assert resp.status_code == 200
    assert b"Aggregate" in resp.body


@pytest.mark.asyncio
async def test_api_categories_endpoint():
    from main import api_categories
    data = await api_categories()
    assert data["total"] > 0
    slugs = {c["slug"] for c in data["categories"]}
    assert "resistors" in slugs
    resistors = next(c for c in data["categories"] if c["slug"] == "resistors")
    assert resistors["count"] > 0


@pytest.mark.asyncio
async def test_api_suggest_endpoint():
    from main import api_suggest
    data = await api_suggest(q="esp3", limit=5)
    assert data["suggestions"]
    assert data["suggestions"][0]["type"] in ("mpn", "product", "category")


@pytest.mark.asyncio
async def test_api_search_supports_category_filter():
    data = await api_search(q="resistor", source="local", limit=20, category="resistors")
    assert data.comparison_groups
    for group in data.comparison_groups:
        assert group.category_canonical == "resistors"


@pytest.mark.asyncio
async def test_api_search_browse_mode_without_query():
    data = await api_search(q="", source="local", limit=20, category="resistors")
    assert data.total_results > 0
    assert data.comparison_groups
