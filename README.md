# RaceGuard

[![CI](https://github.com/raceguard/raceguard/actions/workflows/ci.yml/badge.svg)](https://github.com/raceguard/raceguard/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/raceguard.svg)](https://pypi.org/project/raceguard/)
[![Python](https://img.shields.io/pypi/pyversions/raceguard.svg)](https://pypi.org/project/raceguard/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

RaceGuard finds, proves, and repairs atomicity violations in backend HTTP
handlers that read shared database state, decide on it, and write it back
without holding anything that prevents a concurrent request slipping into the
gap.

```
T1: SELECT stock FROM items WHERE id = 1;   -- reads 1
T2: SELECT stock FROM items WHERE id = 1;   -- also reads 1
T1: (stock >= qty) -> true
T2: (stock >= qty) -> true
T1: UPDATE items SET stock = 0 WHERE id = 1;
T2: UPDATE items SET stock = 0 WHERE id = 1;
```

Nothing there is a syntax error, a type error, or a lint violation. The defect
is the interval between the read and the write, and that interval is invisible
to any tool that evaluates one request at a time.

## The promise, and its limits

RaceGuard says one of three things about a target, and never anything stronger:

1. This handler contains a read-decide-write window with no guard on the path,
   at stated confidence.
2. Under N concurrent requests with this seed, the stated invariant was
   violated in K of T trials.
3. This patch removes the window, and after applying it the invariant held in
   T of T trials and your own test suite still passed.

**It never says "safe", "secure", or "verified."** One violating trial is a
proof of the defect. Zero violating trials is evidence bounded by the trial
count, the seed, the concurrency, the boot mode and the amplification setting.
There is a test in this repository asserting that the word never appears in
report output.

## Sixty seconds

```bash
pip install "raceguard[harness]"

cd examples/shop
raceguard scan .                      # static, no Docker, milliseconds
raceguard verify --target purchase    # boots PostgreSQL, fires real requests
raceguard fix --target purchase       # prints a diff, writes nothing
```

The reference app sells one item to two concurrent buyers. Both are accepted,
both orders are recorded, and stock ends at zero: two units sold from a stock
of one. The generated patch moves the comparison into the `UPDATE`, after which
exactly one buyer wins every time.

## Installing

```bash
pip install raceguard                 # scan and the hygiene rules
pip install "raceguard[harness]"      # + dynamic verification (needs Docker)
pip install "raceguard[mcp]"          # + the MCP server
```

Or without installing Python tooling at all:

```bash
# scan needs no Docker socket
docker run --rm -v "$PWD:/src:ro" -w /src ghcr.io/raceguard/raceguard scan .

# verify does; mount it deliberately
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD:/src" -w /src \
  ghcr.io/raceguard/raceguard verify --target purchase
```

### In CI

```yaml
- uses: raceguard/raceguard@v1
  with:
    path: .
    fail-on: high
    verify: false          # true also verifies targets whose handler changed
- uses: github/codeql-action/upload-sarif@v3
  with:
    sarif_file: raceguard.sarif
```

Any other CI system calls the same engine directly:

```bash
raceguard ci --base origin/main --sarif raceguard.sarif   # exit 1 on gated findings
```

GitLab users include `ci/gitlab/raceguard.gitlab-ci.yml`. Adopting on a
codebase with existing findings: `raceguard baseline write`, commit the file,
pass `--baseline`. See [docs/ci.md](docs/ci.md).

### As an MCP server

```bash
raceguard mcp install claude-code --write      # also: claude-desktop, cursor, vscode
raceguard mcp serve --transport http --port 8765   # hosted, with RACEGUARD_MCP_TOKENS
```

Every path is sandboxed to the project root, verification is budgeted per
client, and `/healthz`, `/readyz` and `/metrics` are there for whoever runs
it. See [docs/mcp.md](docs/mcp.md).

### As a pre-commit hook

```yaml
repos:
  - repo: https://github.com/raceguard/raceguard
    rev: v0.1.0
    hooks:
      - id: raceguard
```

Scan only, staged files only, high confidence only, no Docker. The verify step
never belongs in a pre-commit hook: a thirty second wait on commit gets the
hook removed, and a hook that has been removed protects nothing.

## What is in scope for v1

| Dimension | Supported |
|---|---|
| Language | Python 3.11, 3.12, 3.13 |
| Framework | FastAPI and Starlette routing, `def` and `async def` handlers |
| Data access | SQLAlchemy 2.x ORM and Core, plus raw `text()` SQL |
| Database | PostgreSQL 14 to 17 |
| Migrations | Alembic, or any shell command you supply |
| Delivery | CLI, MCP server, GitHub Action, pre-commit hook |

**Explicitly out of scope for v1**, so nobody has to open an issue to find out:
Django ORM, Flask with raw psycopg, MySQL, SQLite, MongoDB, Redis-backed locks,
Celery and other queue consumers, gRPC, GraphQL resolvers, and multi-service
distributed transactions. Each becomes a v2 adapter behind the same contracts.

## The five patterns

| Pattern | Shape | Usual fix |
|---|---|---|
| `oversell` | counter compared to a threshold, then decremented | atomic `UPDATE ... WHERE` |
| `lost_update` | value read and written back, no decision between | row lock or version column |
| `single_use` | one-time value checked then consumed | conditional `UPDATE` |
| `duplicate_insert` | create-if-absent with no unique constraint | `ON CONFLICT` **plus a migration** |
| `aggregate_limit` | count across rows checked, then a row added | advisory lock keyed by the group |

## Recognised guards

A window is only a finding if nothing on the path protects it. RaceGuard
recognises `with_for_update`, atomic expressions in `values()`, `ON CONFLICT`,
`FOR UPDATE` inside raw SQL, advisory locks, a covering unique constraint, a
version check in the `WHERE` clause, and SERIALIZABLE isolation.

SERIALIZABLE **without a retry wrapper is a partial guard, not a guard.** It
converts silent data corruption into user-visible 500s, which is better but not
correct, so it is reported at reduced confidence rather than cleared.

## Two numbers, never merged

Every target is verified twice:

* **amplified** — a delay is inserted after the first read of the table under
  test. This proves the window exists and can be entered. It is the structural
  claim.
* **natural** — no help at all. This measures how often it fires unaided. It is
  the operational claim.

They are reported separately on purpose. The difference between "your code has
a race window" and "your code loses money three times in a hundred" is the
whole difference between an honest tool and an alarming one.

## RaceGuard governs itself

RaceGuard is a load generator by construction. Left ungoverned it is
indistinguishable from an attack. The governor is mandatory, not a setting: a
global concurrency ceiling, a minimum pause between trials, a hard per-run
request budget, a token bucket on the outbound side, backpressure that stops on
429 or 503 and honours `Retry-After`, and an ownership check that refuses to
fire at a database RaceGuard did not create. Overriding that check requires
typing the host name, and even then the ceiling drops and amplification is
disabled.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Completed, nothing above the configured gate |
| 1 | Findings or violations above the gate |
| 2 | Configuration or usage error |
| 3 | Infrastructure failure (Docker unavailable, image pull failed, budget exceeded) |
| 4 | Interrupted |

CI systems distinguish these. Conflating them makes a broken runner look like a
security finding.

## Development

```bash
make setup     # venv plus every extra
make check     # lint, types, tests, and the quality gates, exactly as CI runs them
make gates     # just the scanner recall and false positive numbers
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the rules that are enforced by
tests rather than by review.

## Documentation

* [Configuration reference](docs/configuration.md)
* [What reproduction rate does and does not mean](docs/reproduction-rate.md)
* [The recognised guard list](docs/guards.md)
* [Suppression](docs/suppression.md)
* [The remediation catalogue](docs/remediation.md)
* [Security and the sandbox](docs/security.md)
* [Security policy and threat model](SECURITY.md)
* [Changelog](CHANGELOG.md)

## Licence

Apache-2.0.
