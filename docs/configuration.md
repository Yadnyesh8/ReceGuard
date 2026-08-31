# Configuration reference

Nobody hand-writes this file. `raceguard init` generates it from a scan: routes
from decorators, payloads from the endpoint's Pydantic request model, seed rows
from the SQLAlchemy model with boundary values filled in, and invariants from
schema constraints. You then edit auth and the specifics.

Schema version 1 keeps working across all 1.x releases.

```yaml
version: 1

app:
  module: "shop.main:app"                    # for inprocess mode
  start: "uvicorn shop.main:app --host 0.0.0.0 --port 8000 --workers 4"
  dockerfile: "Dockerfile"                   # optional, for container mode
  health_path: "/health"
  base_url: "http://localhost:8000"
  database_url_env: "DATABASE_URL"
  database_url_driver: "asyncpg"             # see note below
  migrate: "alembic upgrade head"
  test_command: "pytest -q"
  thread_pool_size: 40                       # informational; warns on concurrency

database:
  image: "postgres:17-alpine@sha256:<digest>"
  extensions: ["pgcrypto"]

defaults:
  mode: container                            # container | inprocess
  trials: 20
  concurrency: 8
  sync: barrier                              # barrier | last_byte | single_packet
  amplify: true
  amplify_ms: 25
  trial_timeout_s: 30
  target_budget_s: 300

governor:
  max_global_concurrency: 16
  min_trial_spacing_ms: 250
  max_requests_per_run: 5000
  respect_rate_limit_headers: true

synth:
  provider: none                             # none | claude | openai | command
  critic_enabled: true

targets:
  - id: purchase
    route: "POST /orders"
    handler: "shop/routes/orders.py::create_order"
    spec: "an order must never reduce stock below zero"
    payload: { item_id: 1, qty: 1 }
    headers: { Authorization: "Bearer ${RG_TEST_TOKEN}" }
    seed:
      items:
        - { id: 1, sku: "ABC", stock: 1 }
    invariant:
      kind: non_negative
      table: items
      column: stock
    concurrency: 2
    trials: 30

hygiene:
  enabled: true

suppress:
  - handler: "shop/routes/admin.py::bulk_import"
    rule: oversell
    reason: "single admin caller, serialised upstream by the import queue"
```

## `app.database_url_driver`

The SQLAlchemy driver your application expects in its URL, such as `asyncpg` or
`psycopg`. Leave it unset for a synchronous app and the URL arrives as plain
`postgresql://`.

RaceGuard does **not** guess this. An async app handed a sync URL fails at
import with an error naming `psycopg2`, which points nowhere near the actual
problem.

## Seeding philosophy

Use the tightest state that can expose the invariant. `stock = 1` with
concurrency 2 is a far better signal than `stock = 100` with concurrency 50,
because the final state distinguishes a violation cleanly rather than burying
it in noise. `raceguard init` generates tight seeds automatically.

## Choosing the invariant kind

| Kind | Check |
|---|---|
| `non_negative` | `SELECT ... WHERE column < 0` |
| `conservation` | `SELECT SUM(col) ... HAVING SUM(col) <> :expected` |
| `single_use` | `GROUP BY key HAVING COUNT(*) > 1` |
| `duplicate_insert` | `GROUP BY natural_key HAVING COUNT(*) > 1` |
| `aggregate_limit` | grouped aggregate compared against the limit |

**The one that catches people out.** If a handler decrements through an ORM
attribute (`item.stock -= qty`) rather than in SQL, two concurrent requests
both write the *same* value: stock goes 1 → 0 twice, never to −1. A
`non_negative` check therefore **holds** while two units were sold from a stock
of one. Use a `conservation` check comparing the amount remaining against the
work recorded. RaceGuard warns when a run looks like this: the invariant held
but more requests succeeded than the seed allowed.

`check_sql` may always be written by hand, and it must return **rows only when
the invariant is violated**. That convention is enforced in one place and every
generated check follows it.

## Environment variables

`${VAR}` is substituted at load time; `${VAR:-default}` supplies a fallback. An
undefined variable with no default is an error, not an empty string. Resolved
values never reach run artifacts.

## The multi-endpoint shape

`group:` reserves the config key for two different endpoints fired together,
where the collision only happens when A and B overlap. v1 fires the group; the
invariant machinery is unchanged.

```yaml
  - id: checkout_race
    group:
      - { route: "POST /cart/apply-coupon", payload: { code: "SAVE10" } }
      - { route: "POST /cart/checkout", payload: {} }
    invariant:
      kind: single_use
      table: coupon_redemptions
```
