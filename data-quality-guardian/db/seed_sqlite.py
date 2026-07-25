"""Create and seed the SQLite "system of record" for a tiny e-commerce shop.

The script is deliberately *idempotent*: it drops and recreates every table, so
you can run it as often as you want and always start from the same known state.

After seeding clean data it INTENTIONALLY injects four defects.  Those defects
are what the agent loop has to find and repair:

  D1  orphan order          - orders.customer_id points at a deleted customer
  D2  orphan order_item     - order_items.product_id points at a missing product
  D3  negative stock        - products.stock < 0
  D4  price mismatch        - order_items.unit_price != products.price

Note: foreign keys are declared for documentation, but PRAGMA foreign_keys is
left OFF while seeding so the broken rows can actually be inserted.  That is
exactly how real legacy databases end up dirty.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import SQLITE_PATH  # noqa: E402

SCHEMA = """
DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS customers;

CREATE TABLE customers (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    email TEXT NOT NULL
);

CREATE TABLE products (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    price REAL NOT NULL,
    stock INTEGER NOT NULL
);

CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    order_date  TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE TABLE order_items (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity   INTEGER NOT NULL,
    unit_price REAL NOT NULL,
    FOREIGN KEY (order_id)   REFERENCES orders(id),
    FOREIGN KEY (product_id) REFERENCES products(id)
);
"""


def seed(db_path: Path = SQLITE_PATH) -> Path:
    """(Re)create shop.db with clean rows plus four intentional defects."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)

        # ---------------- clean, valid data ----------------
        conn.executemany(
            "INSERT INTO customers (id, name, email) VALUES (?, ?, ?)",
            [
                (1, "Ada Lovelace", "ada@example.com"),
                (2, "Alan Turing", "alan@example.com"),
                (3, "Grace Hopper", "grace@example.com"),
            ],
        )
        conn.executemany(
            "INSERT INTO products (id, name, price, stock) VALUES (?, ?, ?, ?)",
            [
                (10, "Mechanical Keyboard", 99.00, 25),
                (11, "USB-C Hub", 45.50, 10),
                (12, "Noise Cancelling Headphones", 199.99, 5),
            ],
        )
        conn.executemany(
            "INSERT INTO orders (id, customer_id, order_date) VALUES (?, ?, ?)",
            [
                (100, 1, "2024-01-05"),
                (101, 2, "2024-01-07"),
                (102, 3, "2024-02-11"),
            ],
        )
        conn.executemany(
            "INSERT INTO order_items (id, order_id, product_id, quantity, unit_price)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (1000, 100, 10, 1, 99.00),
                (1001, 101, 11, 2, 45.50),
                (1002, 102, 12, 1, 199.99),
            ],
        )

        # ---------------- intentional defects ----------------
        # D1: an order whose customer (id 99) does not exist - "deleted customer".
        conn.execute(
            "INSERT INTO orders (id, customer_id, order_date) VALUES (103, 99, '2024-03-01')"
        )
        conn.execute(
            "INSERT INTO order_items (id, order_id, product_id, quantity, unit_price)"
            " VALUES (1003, 103, 10, 1, 99.00)"
        )

        # D2: an order_item pointing at a product (id 77) that was never created.
        conn.execute(
            "INSERT INTO order_items (id, order_id, product_id, quantity, unit_price)"
            " VALUES (1004, 100, 77, 3, 19.99)"
        )

        # D3: negative stock - impossible in the physical world.
        conn.execute(
            "INSERT INTO products (id, name, price, stock) VALUES (13, 'Laptop Stand', 39.00, -4)"
        )

        # D4: unit_price disagrees with the catalogue price of product 11.
        conn.execute(
            "INSERT INTO order_items (id, order_id, product_id, quantity, unit_price)"
            " VALUES (1005, 102, 11, 1, 12.00)"
        )

        conn.commit()
    finally:
        conn.close()
    return db_path


if __name__ == "__main__":
    path = seed()
    print(f"Seeded {path} with clean rows + 4 intentional defects.")
