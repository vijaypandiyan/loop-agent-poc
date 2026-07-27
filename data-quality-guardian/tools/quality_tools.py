"""The "hands" of the system: plain Python functions the agents can call.

Every function here is an ordinary, testable Python function.  ADK turns them
into callable tools automatically (``FunctionTool(func=...)``) by reading the
signature and the docstring - which is why the docstrings below are written for
an LLM audience: they describe *when* to call the tool and *what* comes back.

Three tools:
    detect_issues()  - read-only.  Cross-checks SQLite AND the Kuzu graph.
    count_issues()   - read-only.  Also flips the loop's escalate flag at zero.
    apply_fix(...)   - write.  Repairs one issue and re-syncs the graph.
"""

import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import KUZU_PATH, SQLITE_PATH  # noqa: E402
from graph.build_kuzu import query as cypher  # noqa: E402
from graph.build_kuzu import sync_from_sqlite  # noqa: E402

# ---------------------------------------------------------------------------
# Cypher queries.  Each one answers a question that is awkward in SQL but
# natural on a graph: "which node is missing an edge?"
# ---------------------------------------------------------------------------

# An order nobody placed => the Customer row was deleted underneath it.
Q_ORPHAN_ORDERS = """
MATCH (o:`Order`)
WHERE NOT EXISTS { MATCH (:Customer)-[:PLACED]->(o) }
RETURN o.id
"""

# The graph can only link an order_item to a Product that exists, so an order
# whose CONTAINS edge count is lower than its SQLite item_count is hiding at
# least one order_item that points at a missing product.
Q_ORDERS_WITH_MISSING_ITEM_EDGES = """
MATCH (o:`Order`)
OPTIONAL MATCH (o)-[c:CONTAINS]->(:Product)
WITH o, count(c) AS linked
WHERE linked < o.item_count
RETURN o.id, o.item_count, linked
"""

# Impossible inventory.
Q_NEGATIVE_STOCK = """
MATCH (p:Product)
WHERE p.stock < 0
RETURN p.id, p.stock
"""

# The price frozen on the edge disagrees with the catalogue price on the node.
Q_PRICE_MISMATCH = """
MATCH (:`Order`)-[c:CONTAINS]->(p:Product)
WHERE abs(c.unit_price - p.price) > 0.001
RETURN c.item_id, c.unit_price, p.price
"""


def _sqlite() -> sqlite3.Connection:
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def detect_issues() -> dict[str, Any]:
    """Scan the shop database for data-quality and referential-integrity defects.

    Runs SQL integrity checks against SQLite *and* Cypher queries against the
    Kuzu graph mirror, then merges the results.

    Returns:
        A dict with ``count`` and ``issues``.  Each issue has:
        ``issue_type`` (orphan_order | orphan_order_item | negative_stock |
        price_mismatch), ``table``, ``primary_key``, ``description`` and
        ``suggested_fix``.
    """
    issues: list[dict[str, Any]] = []
    conn = _sqlite()
    try:
        # --- graph pass: orders with no PLACED edge -------------------------
        graph_orphan_orders = {row[0] for row in cypher(Q_ORPHAN_ORDERS, KUZU_PATH)}
        # --- sql pass: same question, LEFT JOIN style -----------------------
        sql_orphan_orders = {
            r["id"]
            for r in conn.execute(
                "SELECT o.id FROM orders o "
                "LEFT JOIN customers c ON c.id = o.customer_id "
                "WHERE c.id IS NULL"
            )
        }
        for order_id in sorted(graph_orphan_orders | sql_orphan_orders):
            issues.append(
                {
                    "issue_type": "orphan_order",
                    "table": "orders",
                    "primary_key": order_id,
                    "description": (
                        f"Order {order_id} references a customer that does not exist "
                        f"(no PLACED edge in the graph)."
                    ),
                    "suggested_fix": "Delete the orphan order and its order_items.",
                }
            )

        # --- order_items whose product (or order) vanished -------------------
        # The graph tells us *which orders* are affected; SQLite tells us which
        # exact rows.  Together: precise, verifiable repair targets.
        affected_orders = {row[0] for row in cypher(Q_ORDERS_WITH_MISSING_ITEM_EDGES, KUZU_PATH)}
        for r in conn.execute(
            "SELECT oi.id, oi.order_id, oi.product_id FROM order_items oi "
            "LEFT JOIN products p ON p.id = oi.product_id "
            "LEFT JOIN orders  o ON o.id = oi.order_id "
            "WHERE p.id IS NULL OR o.id IS NULL"
        ):
            corroborated = r["order_id"] in affected_orders
            issues.append(
                {
                    "issue_type": "orphan_order_item",
                    "table": "order_items",
                    "primary_key": r["id"],
                    "description": (
                        f"order_item {r['id']} references missing product "
                        f"{r['product_id']} or missing order {r['order_id']}"
                        + (" (confirmed by the graph edge count)." if corroborated else ".")
                    ),
                    "suggested_fix": "Delete the dangling order_item row.",
                }
            )

        # --- negative stock ---------------------------------------------------
        for row in cypher(Q_NEGATIVE_STOCK, KUZU_PATH):
            product_id, stock = row[0], row[1]
            issues.append(
                {
                    "issue_type": "negative_stock",
                    "table": "products",
                    "primary_key": product_id,
                    "description": f"Product {product_id} has negative stock ({stock}).",
                    "suggested_fix": "Clamp stock to 0.",
                }
            )

        # --- unit_price vs catalogue price -----------------------------------
        for row in cypher(Q_PRICE_MISMATCH, KUZU_PATH):
            item_id, unit_price, price = row[0], row[1], row[2]
            issues.append(
                {
                    "issue_type": "price_mismatch",
                    "table": "order_items",
                    "primary_key": item_id,
                    "description": (
                        f"order_item {item_id} unit_price {unit_price} != product price {price}."
                    ),
                    "suggested_fix": "Set unit_price to the catalogue price.",
                }
            )
    finally:
        conn.close()

    return {"count": len(issues), "issues": issues}


def count_issues(tool_context: Any = None) -> dict[str, Any]:
    """Count the data-quality issues that are still present.

    Call this to decide whether the autonomous fix loop should stop.  When the
    count reaches zero this tool sets ``escalate`` on the invocation, which is
    the signal a LoopAgent uses to break out of the loop.

    Returns:
        A dict with ``remaining`` (int) and ``clean`` (bool).
    """
    remaining = detect_issues()["count"]
    clean = remaining == 0
    # ``tool_context`` is injected by ADK; ``actions.escalate`` bubbles up as an
    # EventActions escalate signal that terminates the enclosing LoopAgent.
    if clean and tool_context is not None:
        tool_context.actions.escalate = True
    return {"remaining": remaining, "clean": clean}


# Which row an issue points at, so we can photograph it before and after the fix.
_ROW_LOOKUP = {
    "orphan_order": (
        "SELECT * FROM orders WHERE id = ?",
        "SELECT * FROM order_items WHERE order_id = ?",
    ),
    "orphan_order_item": ("SELECT * FROM order_items WHERE id = ?", None),
    "negative_stock": ("SELECT * FROM products WHERE id = ?", None),
    "price_mismatch": ("SELECT * FROM order_items WHERE id = ?", None),
}


def _snapshot(conn: sqlite3.Connection, issue_type: str, primary_key: int) -> list[dict[str, Any]]:
    """Return the row(s) an issue refers to, as plain dicts (empty list if gone)."""
    queries = _ROW_LOOKUP.get(issue_type)
    if not queries:
        return []
    rows: list[dict[str, Any]] = []
    for sql in queries:
        if sql:
            rows.extend(dict(r) for r in conn.execute(sql, (primary_key,)))
    return rows


def apply_fix(issue_type: str, primary_key: int) -> dict[str, Any]:
    """Repair exactly one detected issue in SQLite, then re-sync the graph.

    Args:
        issue_type: one of ``orphan_order``, ``orphan_order_item``,
            ``negative_stock``, ``price_mismatch`` (the ``issue_type`` field of
            an issue returned by ``detect_issues``).
        primary_key: the ``primary_key`` field of that same issue.

    Returns:
        A dict with ``status`` (fixed | error), the SQL that ran, how many rows
        changed, and a ``before`` / ``after`` photograph of the affected row(s)
        so the repair can be audited.
    """
    conn = _sqlite()
    statements: list[str] = []
    before = _snapshot(conn, issue_type, primary_key)
    issues_before = detect_issues()["count"]
    try:
        if issue_type == "orphan_order":
            # Remove the children first, then the order itself.
            conn.execute("DELETE FROM order_items WHERE order_id = ?", (primary_key,))
            conn.execute("DELETE FROM orders WHERE id = ?", (primary_key,))
            statements = [
                f"DELETE FROM order_items WHERE order_id = {primary_key}",
                f"DELETE FROM orders WHERE id = {primary_key}",
            ]
        elif issue_type == "orphan_order_item":
            conn.execute("DELETE FROM order_items WHERE id = ?", (primary_key,))
            statements = [f"DELETE FROM order_items WHERE id = {primary_key}"]
        elif issue_type == "negative_stock":
            conn.execute("UPDATE products SET stock = 0 WHERE id = ? AND stock < 0", (primary_key,))
            statements = [f"UPDATE products SET stock = 0 WHERE id = {primary_key}"]
        elif issue_type == "price_mismatch":
            conn.execute(
                "UPDATE order_items SET unit_price = ("
                "  SELECT price FROM products WHERE products.id = order_items.product_id"
                ") WHERE id = ? AND EXISTS ("
                "  SELECT 1 FROM products WHERE products.id = order_items.product_id)",
                (primary_key,),
            )
            statements = [
                f"UPDATE order_items SET unit_price = products.price WHERE id = {primary_key}"
            ]
        else:
            return {"status": "error", "message": f"unknown issue_type '{issue_type}'"}

        changed = conn.total_changes
        conn.commit()
        after = _snapshot(conn, issue_type, primary_key)
    finally:
        conn.close()

    # Keep the analytical mirror honest: rebuild it from the repaired truth.
    sync_from_sqlite(SQLITE_PATH, KUZU_PATH)

    return {
        "status": "fixed",
        "issue_type": issue_type,
        "primary_key": primary_key,
        "sql": statements,
        "rows_changed": changed,
        "before": before or "(row absent)",
        "after": after or "(row deleted)",
        "issues_before": issues_before,
        "issues_after": detect_issues()["count"],
    }


if __name__ == "__main__":
    import json

    print(json.dumps(detect_issues(), indent=2))
