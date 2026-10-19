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
    from main import web_ui
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    req = Request(scope)
    resp = await web_ui(request=req, q="esp32", source="local")
    assert resp.status_code == 200
    assert b"Octopart" in resp.body
