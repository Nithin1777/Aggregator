"""Exact Robu GraphQL refresh through the engine, without network or catalog writes."""
import asyncio
import time

import pytest

import db
from adapters.robu import RobuAdapter
from adapters.robu_graphql import GRAPHQL_URL
from config import settings
from models import UnifiedProduct
from search_service import AggregatorEngine


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'db_path', tmp_path / 'robu-refresh.db')
    monkeypatch.setattr(settings, 'price_stock_refresh_enabled', True)
    db.init_db()


def product(index=0):
    return UnifiedProduct(vendor='robu', vendor_name='Robu.in', external_id=str(101 + index),
                          title=f'IG52 DC Motor {index}', normalized_title=f'ig52 dc motor {index}',
                          sku=f'SKU-{index}', price=199, available=True,
                          product_url=f'https://robu.in/product/refresh-test-{index}/')


def detail(index=0, **overrides):
    data = dict(id=str(101 + index), url_key=f'refresh-test-{index}',
                name='Vendor title must never overwrite the catalog',
                price={'final': 149, 'regular': 199}, in_stock=False)
    data.update(overrides)
    return data


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class Session:
    def __init__(self, details, fail_after=None, delay=0):
        self.details, self.fail_after, self.delay = details, fail_after, delay
        self.calls = []

    async def post(self, url, *, json, timeout):
        assert url == GRAPHQL_URL
        self.calls.append(json)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise ConnectionError('endpoint unavailable')
        return Response({'data': {
            f'p{i}': {'status': slug in self.details, 'data': self.details.get(slug)}
            for i, slug in enumerate(json['variables'].values())
        }})

    async def close(self):
        pass


def engine_with(session):
    adapter = RobuAdapter()
    adapter._graphql_session = session
    engine = AggregatorEngine()
    engine.adapters = {'robu': adapter}
    return engine


def test_graphql_refresh_targets_every_visible_listing_without_text_search(catalog):
    db.upsert_products([product(i) for i in range(23)])
    products = db.get_products_by_ids(list(range(1, 24)))
    session = Session({f'refresh-test-{i}': detail(i) for i in range(23)})
    engine = engine_with(session)

    async def forbidden(*args, **kwargs):
        raise AssertionError('search endpoint and homepage warmup must not be used')

    engine.adapters['robu'].search_live = forbidden
    engine.adapters['robu']._get_session = forbidden
    info = asyncio.run(engine.refresh_price_stock(products, query=None, force=True))
    assert info.status == 'live' and info.refreshed_products == 23
    assert info.lookup_methods == {'robu': 'graphql_productdetails'} and info.queries == []
    assert [len(call['variables']) for call in session.calls] == [10, 10, 3]
    assert all('description' not in call['query'] for call in session.calls)
    stored = db.get_products_by_ids([1])[0]
    assert stored.price == 149 and stored.available is False and stored.price_source == 'live'
    assert stored.title == product().title and stored.sku == product().sku
    assert stored.product_url == product().product_url


@pytest.mark.parametrize('overrides', [
    {'id': '999'}, {'id': None}, {'in_stock': None}, {'in_stock': 'false'},
    {'price': {'final': 0, 'regular': 199}}, {'price': {'final': float('nan')}},
    {'price': {'final': float('inf')}}, {'price': {'final': 'bad'}},
])
def test_invalid_or_different_listing_is_not_reported_as_verified(catalog, overrides):
    db.upsert_products([product()])
    products = db.get_products_by_ids([1])
    session = Session({'refresh-test-0': detail(**overrides)})
    info = asyncio.run(engine_with(session).refresh_price_stock(products, force=True))
    assert info.status == 'cached' and info.refreshed_products == 0
    stored = db.get_products_by_ids([1])[0]
    assert stored.price == 199 and stored.available is True


def test_regular_price_is_usable_when_final_is_null(catalog):
    db.upsert_products([product()])
    session = Session({'refresh-test-0': detail(price={'final': None, 'regular': 150})})
    info = asyncio.run(engine_with(session).refresh_price_stock(db.get_products_by_ids([1]), force=True))
    assert info.refreshed_products == 1
    assert db.get_products_by_ids([1])[0].price == 150


def test_late_batch_failure_preserves_already_verified_prices(catalog):
    db.upsert_products([product(i) for i in range(12)])
    session = Session({f'refresh-test-{i}': detail(i) for i in range(12)}, fail_after=1)
    info = asyncio.run(engine_with(session).refresh_price_stock(db.get_products_by_ids(list(range(1, 13))), force=True))
    assert info.status == 'partial' and info.refreshed_products == 10 and info.cached_products == 2
    assert db.get_products_by_ids([12])[0].price == 199


def test_timeout_is_bounded_and_does_not_fall_back_to_old_robu_search(catalog, monkeypatch):
    db.upsert_products([product()])
    monkeypatch.setattr(settings, 'price_stock_refresh_timeout_seconds', .65)
    session = Session({}, delay=30)
    engine = engine_with(session)
    started = time.monotonic()
    info = asyncio.run(engine.refresh_price_stock(db.get_products_by_ids([1]), force=True))
    assert time.monotonic() - started < 2
    assert info.status == 'cached' and info.vendors_failed == ['robu']
    assert len(session.calls) == 1


def test_deadline_salvages_completed_graphql_batches(catalog, monkeypatch):
    db.upsert_products([product(i) for i in range(12)])
    monkeypatch.setattr(settings, 'price_stock_refresh_timeout_seconds', .65)

    class SlowSecondBatch(Session):
        async def post(self, *args, **kwargs):
            if self.calls:
                await asyncio.sleep(30)
            return await super().post(*args, **kwargs)

    session = SlowSecondBatch({f'refresh-test-{i}': detail(i) for i in range(12)})
    started = time.monotonic()
    info = asyncio.run(engine_with(session).refresh_price_stock(db.get_products_by_ids(list(range(1, 13))), force=True))
    assert time.monotonic() - started < 2
    assert info.status == 'partial' and info.refreshed_products == 10 and info.cached_products == 2
    assert db.get_products_by_ids([1])[0].price == 149
    assert db.get_products_by_ids([12])[0].price == 199


def test_unrepresented_vendors_are_not_contacted(catalog):
    db.upsert_products([product()])
    engine = engine_with(Session({'refresh-test-0': detail()}))

    class Unrelated:
        async def search_live(self, *args, **kwargs):
            raise AssertionError('this vendor has no displayed listings')

    engine.adapters['other'] = Unrelated()
    info = asyncio.run(engine.refresh_price_stock(db.get_products_by_ids([1]), force=True))
    assert info.status == 'live' and info.vendors_ok == ['robu'] and info.vendors_failed == []


def test_mixed_page_refreshes_direct_and_search_vendors_in_the_same_pass(catalog):
    other = product(1).model_copy(update={'vendor': 'other'})
    db.upsert_products([product(), other])
    engine = engine_with(Session({'refresh-test-0': detail()}))

    class Other:
        calls = []

        async def search_live(self, query, limit):
            self.calls.append(query)
            return [other.model_copy(update={'price': 129})]

    engine.adapters['other'] = adapter = Other()
    info = asyncio.run(engine.refresh_price_stock(db.get_products_by_ids([1, 2]), query='IG52', force=True))
    assert info.status == 'live' and info.refreshed_products == 2
    assert info.lookup_methods == {'robu': 'graphql_productdetails', 'other': 'search'}
    assert adapter.calls == ['IG52']


def test_mcp_uses_the_direct_graphql_price_refresh(catalog, monkeypatch):
    db.upsert_products([product()])
    from mcp_server import server
    monkeypatch.setattr(server, '_description_schema_ready', True)
    monkeypatch.setattr(server, '_engine', engine_with(Session({'refresh-test-0': detail()})))
    result = server.search_components(query='IG52', force_refresh=True)
    assert result.ok and result.data['results'][0]['price'] == 149
    assert result.data['price_stock_refresh']['lookup_methods'] == {'robu': 'graphql_productdetails'}
