"""Many customers buying at the same moment. Deliberately knows nothing about RaceGuard.

Plain HTTP against a running server, plain SQL to count the result. No injected
delay and no amplification, so whatever this shows is what the app does when
many people click at once. demo.py calls it before and after the patch.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import psycopg


@dataclass
class Outcome:
    statuses: list[int]
    accepted: int
    rows: int
    stock: int

    @property
    def oversold(self) -> bool:
        return self.rows > 1


async def _buy(client: httpx.AsyncClient, path: str, body: dict) -> int:
    response = await client.post(path, json=body)
    return response.status_code


async def _fire(base_url: str, path: str, body: dict, buyers: int) -> list[int]:
    limits = httpx.Limits(max_connections=buyers)
    async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=30) as client:
        # Open every connection first, so the buyers below arrive together.
        await asyncio.gather(*(client.get("/health") for _ in range(buyers)))
        return list(await asyncio.gather(*(_buy(client, path, body) for _ in range(buyers))))


def order_item(base_url: str, dsn: str, buyers: int) -> Outcome:
    """One unit of stock, many buyers, POST /orders: the vulnerable endpoint."""
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute("TRUNCATE items, orders RESTART IDENTITY")
        db.execute("INSERT INTO items (id, sku, stock) VALUES (1, 'ABC-1', 1)")

    statuses = asyncio.run(_fire(base_url, "/orders", {"item_id": 1, "qty": 1}, buyers))

    with psycopg.connect(dsn, autocommit=True) as db:
        rows = db.execute("SELECT count(*) FROM orders").fetchone()[0]
        stock = db.execute("SELECT stock FROM items WHERE id = 1").fetchone()[0]
    return Outcome(statuses, statuses.count(200), rows, stock)


def purchase_product(base_url: str, dsn: str, buyers: int) -> Outcome:
    """The same rush on POST /products/1/purchase, which uses one atomic UPDATE.

    This endpoint records no order rows, so `rows` counts accepted requests.
    """
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute("TRUNCATE products RESTART IDENTITY")
        db.execute("INSERT INTO products (id, sku, stock) VALUES (1, 'XYZ-9', 1)")

    statuses = asyncio.run(_fire(base_url, "/products/1/purchase", {"qty": 1}, buyers))

    with psycopg.connect(dsn, autocommit=True) as db:
        stock = db.execute("SELECT stock FROM products WHERE id = 1").fetchone()[0]
    return Outcome(statuses, statuses.count(200), statuses.count(200), stock)
