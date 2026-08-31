# shop

The reference app for RaceGuard. A small FastAPI + SQLAlchemy 2.x (async) +
PostgreSQL shop with five real, reproducible race conditions and one
correctly guarded endpoint that proves the tool does not just flag
everything that touches a database.

## The sixty second path

```
pip install raceguard
cd examples/shop
raceguard scan .
raceguard verify --target purchase
```

The scan finds the oversell window in `shop/routes/orders.py` statically, in
milliseconds. `verify` boots an ephemeral PostgreSQL, seeds one item with
`stock=1`, fires two concurrent purchase requests, and shows you a real
oversell: two orders accepted against one unit of stock.

## What is here, and why it is broken on purpose

| Endpoint | File | Invariant | The bug |
|---|---|---|---|
| `POST /orders` | `shop/routes/orders.py` | `non_negative` | reads `items.stock`, decides in Python, decrements -- no lock, no atomic expression, no `CHECK(stock >= 0)` |
| `POST /wallet/transfer` | `shop/routes/wallet.py` | `conservation` | reads both balances, decides, writes both -- a lost update can change the pair's total |
| `POST /coupons/redeem` | `shop/routes/coupons.py` | `single_use` | counts prior redemptions, decides, inserts -- no unique index ties a redemption to its coupon |
| `POST /signup` | `shop/routes/signup.py` | `duplicate_insert` | looks up by email, decides, inserts -- `users.email` has no unique index |
| `POST /teams/{id}/members` | `shop/routes/teams.py` | `aggregate_limit` | counts members, decides, inserts -- nothing caps rows per team |
| `POST /products/{id}/purchase` | `shop/routes/safe.py` | `non_negative` | **correctly guarded**: one atomic `UPDATE ... WHERE stock >= qty`, decision and write in the same statement |

`shop/routes/teams.py` is also the app's one plain `def` handler (every
other handler is `async def`). FastAPI runs `def` handlers in a bounded
thread pool instead of on the event loop; see the docstring there and
implementation plan section 8.2.

The schema (`migrations/versions/0001_initial.py`) leaves out exactly the
constraint that would make each bug impossible -- no `CHECK(stock >= 0)`,
no unique index on `email`, nothing capping `team_members` per team -- and
adds one real `CHECK` constraint on an unrelated column
(`teams.max_members > 0`) so schema-reflection invariant derivation has a
genuine constraint to find.

## Running it locally

```
pip install -e .[dev]
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/shop
alembic upgrade head
uvicorn shop.main:app --reload
```

## Tests

```
pytest -q
```

`tests/test_smoke.py` only checks that the app imports, every route is
registered, and `/health` responds -- it needs no database. Reproducing the
races themselves is what `raceguard verify` is for.
