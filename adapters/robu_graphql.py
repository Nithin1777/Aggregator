"""
Robu ProductDetails GraphQL client.

Robu's Next.js frontend uses POST /api/proxy/graphql/ with a `productdetails`
query. Unlike the HTML product page, this endpoint is not blocked for us and
returns description/short_description/price/stock in one JSON document.

The query supports aliases, so up to `batch_size` products can be fetched in one
request. That turns 85k page fetches into a few thousand JSON calls.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional

from curl_cffi.requests import Session

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://robu.in/api/proxy/graphql/"
ORIGIN = "https://robu.in"
REFERER = "https://robu.in/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

PRODUCT_FIELDS = """
    status
    type
    code
    message
    data {
      id
      sku
      name
      url_key
      short_description
      description
      price { regular final special discount }
      in_stock
      available_quantity
      categories { id name url_path }
      images { url }
    }
"""


class RobuGraphQLError(RuntimeError):
    """Raised when Robu returns GraphQL errors or an unusable response."""


def build_batch_query(slugs: List[str]) -> Dict[str, Any]:
    """Builds one aliased ProductDetails query for a list of slugs."""
    if not slugs:
        raise ValueError("slugs must not be empty")
    variables: Dict[str, str] = {}
    parts: List[str] = []
    for index, slug in enumerate(slugs):
        variable = f"$s{index}"
        variables[f"s{index}"] = slug
        parts.append(f"p{index}: productdetails(slug: {variable}) {{{PRODUCT_FIELDS}}}")
    query = (
        "query ProductDetailsBatch("
        + ", ".join(f"$s{i}: String!" for i in range(len(slugs)))
        + ") {\n"
        + "\n".join(parts)
        + "\n}"
    )
    return {
        "operationName": "ProductDetailsBatch",
        "query": query,
        "variables": variables,
    }


def extract_detail(payload: Dict[str, Any], index: int) -> Optional[Dict[str, Any]]:
    """Returns one product detail object from a batched response."""
    node = (payload.get("data") or {}).get(f"p{index}") or {}
    if not node.get("status"):
        return None
    data = node.get("data")
    return data if isinstance(data, dict) else None


class RobuGraphQLClient:
    def __init__(self, timeout: int = 45):
        self.timeout = timeout
        self.session = Session(
            impersonate="chrome120",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": ORIGIN,
                "Referer": REFERER,
                "User-Agent": USER_AGENT,
            },
        )

    def fetch_batch(self, slugs: List[str], batch_size: int = 10) -> Dict[str, Dict[str, Any]]:
        """
        Fetches product details for slugs, batching aliases.

        Returns {slug: detail}. Missing/error products are omitted; a transport
        or GraphQL-level error raises so the caller can decide to resume later.
        """
        results: Dict[str, Dict[str, Any]] = {}
        unique = list(dict.fromkeys(str(s) for s in slugs if s))
        for start in range(0, len(unique), max(1, batch_size)):
            chunk = unique[start:start + max(1, batch_size)]
            payload = build_batch_query(chunk)
            try:
                response = self.session.post(
                    GRAPHQL_URL, json=payload, timeout=self.timeout
                )
            except Exception as exc:  # noqa: BLE001 - caller handles retry/stop
                raise RobuGraphQLError(f"Robu GraphQL request failed: {exc}") from exc

            if response.status_code == 429:
                raise RobuGraphQLError("Robu GraphQL rate limited (HTTP 429)")
            if response.status_code != 200:
                raise RobuGraphQLError(
                    f"Robu GraphQL HTTP {response.status_code}: {response.text[:200]}"
                )

            try:
                body = response.json()
            except json.JSONDecodeError as exc:
                raise RobuGraphQLError("Robu GraphQL returned non-JSON") from exc
            if body.get("errors"):
                messages = "; ".join(
                    str(e.get("message", e)) for e in body["errors"][:3]
                )
                raise RobuGraphQLError(f"Robu GraphQL errors: {messages}")

            for index, slug in enumerate(chunk):
                detail = extract_detail(body, index)
                if detail is not None:
                    results[slug] = detail
        return results

    def fetch_one(self, slug: str) -> Optional[Dict[str, Any]]:
        return self.fetch_batch([slug], batch_size=1).get(slug)

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "RobuGraphQLClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def description_html(detail: Dict[str, Any]) -> str:
    """Combines the two description fields in preference order."""
    return str(detail.get("description") or detail.get("short_description") or "")
