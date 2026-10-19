"""
Taxonomy-Driven MCP Query Server for Electronic Components.
Provides generic tools for category discovery, filter introspection, and targeted parametric search.
No hardcoded category names, attribute keys, or filter values in code or descriptions.
"""
import json
import logging
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from mcp.server.fastmcp import FastMCP
import db
from models import UnifiedProduct
from search_service import AggregatorEngine
from src.search.retrieval import search_catalog_ranked
from src.spec.taxonomy_loader import get_taxonomy, load_taxonomy

logger = logging.getLogger(__name__)

# Initialize FastMCP Server
mcp = FastMCP("octopart-india-taxonomy-pilot")
_engine = AggregatorEngine()

def get_db():
    return db.get_db_connection()

def is_advertised(conn: sqlite3.Connection, category: str, key: str) -> Tuple[bool, float, float]:
    """
    Evaluates whether an attribute meets the advertise_min_fill threshold.
    One centralized function called by both list_filters and search_components.
    Returns (is_advertised, fill_rate, threshold).
    """
    taxonomy = get_taxonomy()
    min_fill = float(taxonomy.advertise_min_fill)

    cur = conn.cursor()
    cur.execute("""
        SELECT applicable_count, filled_count
        FROM spec_stats
        WHERE category = ? AND key = ?;
    """, (category, key))
    row = cur.fetchone()
    if not row:
        return False, 0.0, min_fill

    applicable, filled = row[0], row[1]
    fill_rate = (filled / applicable) if applicable > 0 else 0.0
    return fill_rate >= min_fill, fill_rate, min_fill

@mcp.tool()
def list_categories() -> List[Dict[str, Any]]:
    """
    Lists all available component categories and their catalog product counts.
    Call list_filters for valid keys for a specific category.
    """
    taxonomy = get_taxonomy()
    conn = get_db()
    try:
        cur = conn.cursor()
        results = []
        for cat_id, cat in taxonomy.categories.items():
            cur.execute("SELECT COUNT(1) FROM products WHERE spec_category = ?;", (cat_id,))
            cnt = cur.fetchone()[0]
            results.append({
                "id": cat_id,
                "display": cat.display,
                "product_count": cnt
            })
        return results
    finally:
        conn.close()

@mcp.tool()
def list_filters(category: str) -> List[Dict[str, Any]]:
    """
    Returns available advertised filters and their valid values/ranges for a category.
    Read directly from spec_stats. Call list_categories to discover categories.
    """
    taxonomy = get_taxonomy()
    if category not in taxonomy.categories:
        valid_cats = list(taxonomy.categories.keys())
        raise ValueError(f"Unknown category '{category}'. Valid category IDs: {valid_cats}")

    conn = get_db()
    try:
        cur = conn.cursor()
        cat_def = taxonomy.categories[category]
        advertised_filters = []

        for attr_key, attr in cat_def.attributes.items():
            adv, fill_rate, _ = is_advertised(conn, category, attr_key)
            if not adv:
                continue

            cur.execute("""
                SELECT value_counts, value_min, value_max
                FROM spec_stats
                WHERE category = ? AND key = ?;
            """, (category, attr_key))
            stats_row = cur.fetchone()
            val_counts_dict = json.loads(stats_row[0]) if stats_row and stats_row[0] else {}
            val_min = stats_row[1] if stats_row else None
            val_max = stats_row[2] if stats_row else None

            filter_info: Dict[str, Any] = {
                "key": attr_key,
                "display_name": attr.display_name,
                "type": attr.type,
                "unit": attr.unit,
                "fill_rate": round(fill_rate, 4)
            }

            if attr.type == "enum":
                # Top 20 values sorted by count
                top_values = [
                    {"value": v, "count": c}
                    for v, c in list(val_counts_dict.items())[:20]
                ]
                filter_info["values"] = top_values
            elif attr.type in ("integer", "float"):
                filter_info["range"] = {
                    "min": val_min,
                    "max": val_max
                }

            advertised_filters.append(filter_info)

        return advertised_filters
    finally:
        conn.close()

def _validate_filter_syntax(attr_def, key: str, val: str) -> None:
    """Validates that a filter value does not contain illegal embedded units."""
    v_str = str(val).strip()
    # Check for embedded units in numeric / enum values
    if attr_def.unit and v_str.lower().endswith(attr_def.unit.lower()):
        raise ValueError(
            f"Filter value for '{key}' must not include units (got '{val}'). "
            f"Expected form: '{v_str[:-len(attr_def.unit)].strip()}' without unit suffix."
        )

    if attr_def.type in ("integer", "float"):
        # Expect either exact number ("200") or inclusive range ("150-250")
        if re.search(r"[a-zA-Z]", v_str):
            raise ValueError(
                f"Filter value for '{key}' must not include units or text (got '{val}'). "
                f"Expected form: '200' or range '150-250'."
            )
        if "-" in v_str:
            parts = v_str.split("-")
            if len(parts) != 2:
                raise ValueError(f"Invalid range syntax for '{key}': '{val}'. Expected 'min-max'.")
            try:
                float(parts[0].strip())
                float(parts[1].strip())
            except ValueError:
                raise ValueError(f"Invalid numeric values in range for '{key}': '{val}'.")
        else:
            try:
                float(v_str)
            except ValueError:
                raise ValueError(f"Invalid numeric value for '{key}': '{val}'.")

@mcp.tool()
def search_components(
    category: str,
    filters: Optional[Dict[str, str]] = None,
    query: Optional[str] = None,
    price_max: Optional[float] = None,
    in_stock_only: bool = True,
    sort: str = "relevance",
    limit: int = 10
) -> Dict[str, Any]:
    """
    Searches electronic components within a category using advertised filters and optional FTS text query.
    Call list_filters for valid keys for the specified category.
    """
    taxonomy = get_taxonomy()
    if category not in taxonomy.categories:
        valid_cats = list(taxonomy.categories.keys())
        raise ValueError(f"Unknown category '{category}'. Valid category IDs: {valid_cats}")

    cat_def = taxonomy.categories[category]
    conn = get_db()
    try:
        # Validate all filter keys and values
        clean_filters = filters or {}
        advertised_keys_list = [k for k in cat_def.attributes if is_advertised(conn, category, k)[0]]

        for f_key, f_val in clean_filters.items():
            if f_key not in cat_def.attributes:
                raise ValueError(
                    f"Unknown filter '{f_key}' for category '{category}'. "
                    f"Valid keys: {', '.join(sorted(advertised_keys_list))}"
                )

            is_adv, fill_rate, min_fill = is_advertised(conn, category, f_key)
            if not is_adv:
                raise ValueError(
                    f"Filter '{f_key}' is not advertised for category '{category}' "
                    f"(fill rate {fill_rate:.1%} < threshold {min_fill:.1%}). "
                    f"Available keys: {', '.join(sorted(advertised_keys_list))}"
                )

            attr_def = cat_def.attributes[f_key]
            _validate_filter_syntax(attr_def, f_key, f_val)

        # Build parameterized SQL query with one JOIN per filter
        joins = []
        join_params: List[Any] = []
        where_clauses = ["p.spec_category = ?"]
        where_params: List[Any] = [category]

        for i, (f_key, f_val) in enumerate(clean_filters.items()):
            alias = f"ps_{i}"
            attr_def = cat_def.attributes[f_key]
            v_str = str(f_val).strip()

            joins.append(f"JOIN product_specs {alias} ON p.id = {alias}.product_id AND {alias}.key = ?")
            join_params.append(f_key)

            if attr_def.type in ("integer", "float"):
                if "-" in v_str:
                    low_s, high_s = v_str.split("-", 1)
                    low_val = float(low_s.strip())
                    high_val = float(high_s.strip())
                    where_clauses.append(f"{alias}.value_num >= ? AND {alias}.value_num <= ?")
                    where_params.extend([low_val, high_val])
                else:
                    where_clauses.append(f"{alias}.value_num = ?")
                    where_params.append(float(v_str))
            else:
                # Enum / string exact matching
                where_clauses.append(f"{alias}.value = ?")
                where_params.append(v_str)

        if price_max is not None:
            where_clauses.append("p.price <= ?")
            where_params.append(float(price_max))

        if in_stock_only:
            where_clauses.append("p.available = 1")

        # FTS Query integration if query text is provided
        if query and query.strip():
            clean_q = re.sub(r'["\*\(\)\:]', '', query.strip()).strip()
            tokens = clean_q.split()
            if tokens:
                fts_expr = " ".join([f'"{t}"*' for t in tokens])
                joins.append("JOIN products_fts fts ON p.id = fts.rowid")
                where_clauses.append("products_fts MATCH ?")
                where_params.append(fts_expr)

        params = join_params + where_params

        # Sorting logic
        if sort == "price_asc":
            order_by = "ORDER BY p.price ASC"
        else:
            # Relevance: BM25 / RRF / Canonical
            if query and query.strip():
                order_by = "ORDER BY rank"
            else:
                order_by = "ORDER BY p.is_canonical DESC, p.query_ctr DESC, p.available DESC, p.price ASC"

        sql = f"""
            SELECT p.* FROM products p
            {" ".join(joins)}
            WHERE {" AND ".join(where_clauses)}
            {order_by}
            LIMIT 100;
        """

        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()

        if not rows:
            return {
                "count": 0,
                "results": [],
                "hint": "No components matched the specified filters. Try loosening numeric ranges or removing constraints."
            }

        products: List[UnifiedProduct] = []
        for r in rows:
            prod = UnifiedProduct(
                id=r["id"],
                vendor=r["vendor"],
                vendor_name=r["vendor_name"],
                external_id=r["external_id"],
                title=r["title"],
                normalized_title=r["normalized_title"],
                mpn=r["mpn"],
                sku=r["sku"],
                price=float(r["price"]),
                available=bool(r["available"]),
                product_url=r["product_url"],
                image_url=r["image_url"],
                category=r["category"],
                is_canonical=bool(r["is_canonical"]) if "is_canonical" in r.keys() and r["is_canonical"] else False,
                canonical_for=r["canonical_for"] if "canonical_for" in r.keys() else None,
                query_ctr=float(r["query_ctr"]) if "query_ctr" in r.keys() and r["query_ctr"] is not None else None,
                updated_at=str(r["updated_at"]) if r["updated_at"] else None,
                extra=json.loads(r["raw_json"]) if r["raw_json"] else {}
            )
            products.append(prod)

        # Deduplicate & group using existing canonical/cluster grouping from search_service
        comparison_groups = _engine.group_and_compare(products, query=query)

        # Build response items
        results = []
        for grp in comparison_groups[:limit]:
            # Find representative product id
            rep_prod = next((p for p in products if p.title == grp.canonical_name), products[0])
            
            # Fetch extracted specs for this component
            cur.execute("""
                SELECT ps.key, ps.value, ps.value_type, sd.unit
                FROM product_specs ps
                LEFT JOIN spec_definitions sd ON ps.key = sd.key AND sd.category = ?
                WHERE ps.product_id = ?;
            """, (category, rep_prod.id))
            specs_dict = {row[0]: row[1] for row in cur.fetchall()}

            top_offers = [
                {
                    "vendor": o.vendor_name or o.vendor,
                    "price": o.price,
                    "available": o.available,
                    "url": o.product_url
                }
                for o in grp.offers[:3]
            ]

            results.append({
                "product_id": rep_prod.id,
                "title": grp.canonical_name,
                "category": category,
                "specs": specs_dict,
                "best_price": grp.best_price,
                "best_vendor": grp.best_vendor,
                "top_offers": top_offers
            })

        return {
            "count": len(results),
            "results": results
        }
    finally:
        conn.close()

if __name__ == "__main__":
    mcp.run()
