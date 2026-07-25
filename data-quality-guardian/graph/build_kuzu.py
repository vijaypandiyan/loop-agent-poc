"""Build / refresh a Kuzu property graph that mirrors the SQLite database.

Why a graph at all?  Referential integrity is fundamentally a question about
*edges*: "does this order connect to a customer?", "which products are touched
by a broken order?".  In SQL those questions are LEFT JOIN ... IS NULL gymnastics;
in Cypher they are one-liners such as::

    MATCH (o:`Order`) WHERE NOT EXISTS { MATCH (:Customer)-[:PLACED]->(o) } RETURN o.id

Graph model
-----------
    (Customer)-[:PLACED]->(Order)-[:CONTAINS]->(Product)

Only rows whose *both* endpoints exist can become a relationship - that is the
whole trick.  A defect therefore shows up as a **missing edge**, which Cypher
finds trivially.  To also spot order_items whose product row vanished we copy
``item_count`` (the number of order_items in SQLite) onto the Order node and
compare it with the number of CONTAINS edges we were actually able to create.
"""

import shutil
import sqlite3
import sys
from pathlib import Path

import kuzu

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import KUZU_PATH, SQLITE_PATH  # noqa: E402

# NOTE: ``Order`` is a reserved word in Cypher (ORDER BY), so the label must be
# escaped with backticks everywhere it appears.
DDL = [
    "CREATE NODE TABLE Customer(id INT64, name STRING, email STRING, PRIMARY KEY(id))",
    "CREATE NODE TABLE Product(id INT64, name STRING, price DOUBLE, stock INT64, PRIMARY KEY(id))",
    # item_count = how many order_items SQLite has for this order (edges may be fewer).
    "CREATE NODE TABLE `Order`(id INT64, customer_id INT64, order_date STRING, item_count INT64, PRIMARY KEY(id))",
    "CREATE REL TABLE PLACED(FROM Customer TO `Order`)",
    "CREATE REL TABLE CONTAINS(FROM `Order` TO Product, item_id INT64, quantity INT64, unit_price DOUBLE)",
]


def _fetch_all(sqlite_path: Path):
    """Read every table we need out of SQLite in one connection."""
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        customers = conn.execute("SELECT id, name, email FROM customers").fetchall()
        products = conn.execute("SELECT id, name, price, stock FROM products").fetchall()
        orders = conn.execute("SELECT id, customer_id, order_date FROM orders").fetchall()
        items = conn.execute(
            "SELECT id, order_id, product_id, quantity, unit_price FROM order_items"
        ).fetchall()
    finally:
        conn.close()
    return customers, products, orders, items


def sync_from_sqlite(sqlite_path: Path = SQLITE_PATH, kuzu_path: Path = KUZU_PATH) -> Path:
    """Rebuild the whole graph from SQLite.

    The dataset is tiny, so a full rebuild is simpler (and safer) than an
    incremental update.  Call this after *every* fix so the graph never lies.
    """
    # Wipe any previous graph so the rebuild is deterministic.  Depending on the
    # Kuzu version the database on disk is either a single file or a directory,
    # plus a sibling ``.wal`` file - remove whatever is there.
    if kuzu_path.is_dir():
        shutil.rmtree(kuzu_path)
    elif kuzu_path.exists():
        kuzu_path.unlink()
    wal = kuzu_path.with_suffix(kuzu_path.suffix + ".wal")
    if wal.exists():
        wal.unlink()

    db = kuzu.Database(str(kuzu_path))
    conn = kuzu.Connection(db)
    for stmt in DDL:
        conn.execute(stmt)

    customers, products, orders, items = _fetch_all(sqlite_path)

    customer_ids = {c["id"] for c in customers}
    product_ids = {p["id"] for p in products}
    order_ids = {o["id"] for o in orders}

    for c in customers:
        conn.execute(
            "CREATE (:Customer {id: $id, name: $name, email: $email})",
            {"id": c["id"], "name": c["name"], "email": c["email"]},
        )
    for p in products:
        conn.execute(
            "CREATE (:Product {id: $id, name: $name, price: $price, stock: $stock})",
            {"id": p["id"], "name": p["name"], "price": p["price"], "stock": p["stock"]},
        )
    for o in orders:
        n_items = sum(1 for i in items if i["order_id"] == o["id"])
        conn.execute(
            "CREATE (:`Order` {id: $id, customer_id: $cid, order_date: $d, item_count: $n})",
            {"id": o["id"], "cid": o["customer_id"], "d": o["order_date"], "n": n_items},
        )

    # PLACED edges - only when the customer actually exists.  A missing edge here
    # *is* the orphan-order defect.
    for o in orders:
        if o["customer_id"] in customer_ids:
            conn.execute(
                "MATCH (c:Customer {id: $cid}), (o:`Order` {id: $oid}) CREATE (c)-[:PLACED]->(o)",
                {"cid": o["customer_id"], "oid": o["id"]},
            )

    # CONTAINS edges - only when both the order and the product exist.
    for i in items:
        if i["order_id"] in order_ids and i["product_id"] in product_ids:
            conn.execute(
                "MATCH (o:`Order` {id: $oid}), (p:Product {id: $pid}) "
                "CREATE (o)-[:CONTAINS {item_id: $iid, quantity: $q, unit_price: $up}]->(p)",
                {
                    "oid": i["order_id"],
                    "pid": i["product_id"],
                    "iid": i["id"],
                    "q": i["quantity"],
                    "up": i["unit_price"],
                },
            )

    return kuzu_path


def connect(kuzu_path: Path = KUZU_PATH) -> kuzu.Connection:
    """Open a read/write connection to the existing graph."""
    return kuzu.Connection(kuzu.Database(str(kuzu_path)))


def query(cypher: str, kuzu_path: Path = KUZU_PATH) -> list[list]:
    """Run a Cypher query and return the rows as plain Python lists."""
    result = connect(kuzu_path).execute(cypher)
    rows = []
    while result.has_next():
        rows.append(result.get_next())
    return rows


if __name__ == "__main__":
    path = sync_from_sqlite()
    print(f"Built Kuzu graph at {path}")
    print("Orders with no PLACED edge (orphan orders):")
    print(
        query(
            "MATCH (o:`Order`) WHERE NOT EXISTS { MATCH (:Customer)-[:PLACED]->(o) } RETURN o.id"
        )
    )
