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
import time
from typing import Any, Dict, List, Optional

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


class RobuGraphQLTransientError(RobuGraphQLError):
    """Timeout/429/5xx: retryable without changing the query."""


class RobuGraphQLSchemaError(RobuGraphQLError):
    """The GraphQL schema/query is wrong; retrying or splitting will not help."""


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
    def __init__(
        self,
        timeout: int = 60,
        retries: int = 3,
        backoff: float = 2.0,
    ):
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.backoff = max(0.0, float(backoff))
        self.failures: List[str] = []
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

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST with retries for timeout/429/5xx; schema errors fail immediately."""
        last_error = "unknown"
        for attempt in range(self.retries):
            try:
                response = self.session.post(
                    GRAPHQL_URL, json=payload, timeout=self.timeout
                )
            except Exception as exc:  # noqa: BLE001 - classify and retry
                last_error = str(exc)
                if attempt + 1 < self.retries:
                    delay = self.backoff * (2 ** attempt)
                    logger.warning(
                        "Robu GraphQL transport error (attempt %s/%s): %s; retrying in %.1fs",
                        attempt + 1, self.retries, exc, delay,
                    )
                    time.sleep(delay)
                    continue
                raise RobuGraphQLTransientError(
                    f"Robu GraphQL request failed after {self.retries} attempts: {exc}"
                ) from exc

            if response.status_code in (401, 403):
                # Cloudflare/IP block: retry/backoff, then skip the batch rather
                # than killing an overnight run.
                last_error = f"HTTP {response.status_code}"
                if attempt + 1 < self.retries:
                    delay = self.backoff * (2 ** attempt)
                    logger.warning(
                        "Robu GraphQL %s (attempt %s/%s); retrying in %.1fs",
                        last_error, attempt + 1, self.retries, delay,
                    )
                    time.sleep(delay)
                    continue
                raise RobuGraphQLTransientError(
                    f"Robu GraphQL {last_error} after {self.retries} attempts"
                )

            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                if attempt + 1 < self.retries:
                    delay = self.backoff * (2 ** attempt)
                    logger.warning(
                        "Robu GraphQL %s (attempt %s/%s); retrying in %.1fs",
                        last_error, attempt + 1, self.retries, delay,
                    )
                    time.sleep(delay)
                    continue
                raise RobuGraphQLTransientError(
                    f"Robu GraphQL {last_error} after {self.retries} attempts"
                )

            if response.status_code != 200:
                raise RobuGraphQLSchemaError(
                    f"Robu GraphQL HTTP {response.status_code}: {response.text[:200]}"
                )

            try:
                body = response.json()
            except json.JSONDecodeError as exc:
                last_error = "non-JSON response"
                if attempt + 1 < self.retries:
                    time.sleep(self.backoff * (2 ** attempt))
                    continue
                raise RobuGraphQLTransientError(
                    "Robu GraphQL returned non-JSON after retries"
                ) from exc

            if body.get("errors"):
                messages = "; ".join(
                    str(e.get("message", e)) for e in body["errors"][:3]
                )
                raise RobuGraphQLSchemaError(f"Robu GraphQL errors: {messages}")
            return body

        raise RobuGraphQLTransientError(f"Robu GraphQL failed: {last_error}")

    def _fetch_chunk(self, chunk: List[str]) -> Dict[str, Dict[str, Any]]:
        try:
            body = self._post(build_batch_query(chunk))
        except RobuGraphQLTransientError:
            if len(chunk) > 1:
                # One slow/oversized product must not discard the whole batch.
                middle = len(chunk) // 2
                logger.warning(
                    "Robu GraphQL batch of %s failed; splitting into %s + %s",
                    len(chunk), middle, len(chunk) - middle,
                )
                merged: Dict[str, Dict[str, Any]] = {}
                merged.update(self._fetch_chunk(chunk[:middle]))
                merged.update(self._fetch_chunk(chunk[middle:]))
                return merged
            raise

        results: Dict[str, Dict[str, Any]] = {}
        for index, slug in enumerate(chunk):
            detail = extract_detail(body, index)
            if detail is not None:
                results[slug] = detail
        return results

    def fetch_batch(self, slugs: List[str], batch_size: int = 10) -> Dict[str, Dict[str, Any]]:
        """
        Fetches product details for slugs, batching aliases.

        A single failed chunk is split and retried; if one slug still fails it is
        recorded in `self.failures` and the remaining work continues. The caller
        can rerun later because failed slugs stay pending/undescribed.
        """
        self.failures = []
        results: Dict[str, Dict[str, Any]] = {}
        unique = list(dict.fromkeys(str(s) for s in slugs if s))
        for start in range(0, len(unique), max(1, batch_size)):
            chunk = unique[start:start + max(1, batch_size)]
            try:
                results.update(self._fetch_chunk(chunk))
            except RobuGraphQLSchemaError:
                raise
            except RobuGraphQLTransientError as exc:
                logger.warning(
                    "Robu GraphQL skipped %s slug(s) after retries: %s",
                    len(chunk), exc,
                )
                self.failures.extend(chunk)
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
