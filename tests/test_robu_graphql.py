"""Robu ProductDetails GraphQL client tests (no network)."""
import json

from adapters.robu_graphql import (
    RobuGraphQLClient,
    build_batch_query,
    description_html,
    extract_detail,
)


class _Response:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _RetrySession:
    """Fails once, then returns one product detail."""

    def __init__(self):
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("simulated timeout")
        return _Response({
            "data": {"p0": {"status": True, "data": {"name": "Pico"}}}
        })

    def close(self):
        pass


class _SplitSession:
    """Fails for multi-alias queries, succeeds for single-alias queries."""

    def __init__(self):
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        payload = kwargs["json"]
        count = len(payload["variables"])
        if count > 1:
            raise TimeoutError("batch too large")
        slug = next(iter(payload["variables"].values()))
        return _Response({
            "data": {"p0": {"status": True, "data": {"name": slug}}}
        })

    def close(self):
        pass


def test_build_batch_query_aliases_and_variables():
    payload = build_batch_query(["raspberry-pi-pico", "raspberry-pi-pico-2"])

    query = payload["query"]
    assert payload["operationName"] == "ProductDetailsBatch"
    assert "p0: productdetails(slug: $s0)" in query
    assert "p1: productdetails(slug: $s1)" in query
    assert payload["variables"] == {
        "s0": "raspberry-pi-pico",
        "s1": "raspberry-pi-pico-2",
    }


def test_extract_detail_reads_aliased_payload():
    payload = {
        "data": {
            "p0": {
                "status": True,
                "data": {"name": "Pico", "description": "<p>x</p>"},
            },
            "p1": {"status": False, "message": "not found"},
        }
    }

    assert extract_detail(payload, 0)["name"] == "Pico"
    assert extract_detail(payload, 1) is None


def test_description_prefers_description_then_short_description():
    assert description_html(
        {"description": "<p>full</p>", "short_description": "short"}
    ) == "<p>full</p>"
    assert description_html({"description": "", "short_description": "short"}) == "short"
    assert description_html({}) == ""


def test_client_retries_a_transient_timeout():
    client = RobuGraphQLClient(retries=2, backoff=0)
    client.session = _RetrySession()

    result = client.fetch_batch(["raspberry-pi-pico"], batch_size=1)

    assert result["raspberry-pi-pico"]["name"] == "Pico"
    assert client.failures == []


def test_client_splits_a_failing_batch_and_keeps_partial_results():
    client = RobuGraphQLClient(retries=2, backoff=0)
    client.session = _SplitSession()

    result = client.fetch_batch(["a", "b"], batch_size=2)

    assert set(result) == {"a", "b"}
    assert client.failures == []
