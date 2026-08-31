# RaceGuard: full implementation specification

Version 1.0 of this document. Written for a three person team building a production tool, not a demo. Everything here is meant to be executable: file paths, function signatures, config keys, exit codes, quality gates, and a week by week plan.

## 0. How to read this

Sections 1 to 3 are the domain: what the tool detects and what correct concurrency control actually looks like in production systems. Sections 4 to 12 are the build. Sections 13 to 18 are the engineering around the build (safety, statistics, observability, packaging). Section 19 covers where subagents are used, both in your development workflow and inside the product. Sections 20 to 23 are planning, risk, and reference material.

Two conventions used throughout:

* A "target" is one HTTP endpoint plus the seed state, payload, and invariant needed to verify it. Targets live in `raceguard.yaml`.
* A "finding" is a static suspicion. A "verdict" is a dynamic result. Never let those two words blur, in code or in the report.

## 1. Product definition and scope contract

RaceGuard finds, proves, and repairs atomicity violations in backend HTTP handlers that read shared database state, decide on it, and write it back without holding anything that prevents a concurrent request from slipping into the gap.

### 1.1 In scope for v1

| Dimension | v1 support |
|---|---|
| Language | Python 3.11, 3.12, 3.13 |
| Framework | FastAPI and Starlette routing, both `def` and `async def` handlers |
| Data access | SQLAlchemy 2.x ORM and Core, plus raw `text()` SQL |
| Database | PostgreSQL 14 to 17 |
| Migrations | Alembic, or any user supplied shell command |
| Delivery | CLI, MCP server, GitHub Action, pre commit hook |

### 1.2 Explicitly out of scope for v1

Django ORM, Flask with raw psycopg, MySQL, SQLite, MongoDB, Redis backed locks, Celery and other queue consumers, gRPC, GraphQL resolvers, and multi service distributed transactions. Each becomes a v2 adapter behind the same contracts. Write this list into the README on day one; scope questions arriving as GitHub issues cost more than the feature would have.

### 1.3 The promise the tool makes

RaceGuard says one of three things about a target, and never anything stronger:

1. This handler contains a read decide write window with no guard on the path, at stated confidence.
2. Under N concurrent requests with this seed, the stated invariant was violated in K of T trials.
3. This patch removes the window, and after applying it the invariant held in T of T trials and your own test suite still passes.

It never says "safe", "secure", or "verified". A tool that overclaims once loses the room permanently, and reviewers of a final year project will look for exactly that overclaim.

## 2. The concurrency domain: what RaceGuard is actually looking for

This section exists so that the scanner rules, the invariant classes, and the patch strategies all trace back to one taxonomy rather than being invented per module.

### 2.1 The structural pattern

Every bug in scope has the same shape:

```
T1: BEGIN
T1: SELECT stock FROM items WHERE id = 1;   -- reads 1
T2: BEGIN
T2: SELECT stock FROM items WHERE id = 1;   -- also reads 1
T1: (stock >= qty) -> true
T2: (stock >= qty) -> true
T1: UPDATE items SET stock = 0 WHERE id = 1;
T2: UPDATE items SET stock = 0 WHERE id = 1;  -- or -1
T1: COMMIT
T2: COMMIT
```

Under Read Committed, which is the PostgreSQL default, a plain `SELECT` takes no row lock and sees a snapshot taken at statement start. Nothing in the sequence above is a syntax error, a type error, or a lint violation. The defect is the interval between the read and the write, and that interval is invisible to any tool that evaluates one request at a time.

### 2.2 The five invariant classes

Every target maps to exactly one, which keeps the synthesizer, the checker, and the patcher aligned.

**non_negative.** A counter is checked against a threshold and then decremented. Oversell, limit overrun, coupon budget exhaustion. Check: `SELECT ... WHERE column < 0`.

**conservation.** Value moves between rows and the total must not change. Transfers, wallet debits and credits, ledger entries. Check: `SELECT SUM(col) FROM t HAVING SUM(col) <> :expected`.

**single_use.** A token, voucher, invite, or one time action may be consumed once. Check: a `GROUP BY key HAVING COUNT(*) > 1` on the consumption table, or a boolean flag flipped more than once observed through an audit row count.

**duplicate_insert.** A "create if it does not exist" flow with no unique constraint. Signups, idempotent order creation, follow relationships. Check: `GROUP BY natural_key HAVING COUNT(*) > 1`.

**aggregate_limit.** A rule spanning multiple rows: at most five members per team, at most three active sessions, total order value under a credit limit. Check: a grouped aggregate compared against the limit column or constant.

### 2.3 Race shapes beyond the single endpoint

Worth encoding as scanner metadata now even if the harness only exercises the first shape in v1, because the config format has to leave room for them.

*Single endpoint, same request repeated.* The default. N identical requests fired together.

*Multi endpoint.* Two different endpoints touching the same row, where the collision only happens when A and B overlap. Example: `POST /cart/apply-coupon` racing `POST /cart/checkout`. Config needs a `group` of targets fired together rather than one target repeated. Web race condition research calls the exploitable interval between action initiation and commit a sub state, and multi endpoint races are how you reach one.

*Partial construction.* An object exists in the database in a half initialised state during a request, and a second request observes it. Common with a row inserted before a related row, or a flag set before the work completes.

*Time sensitive.* Not concurrency in the strict sense: two requests processed in the same clock tick produce the same token, nonce, or timestamp derived value. Detectable with the same harness and worth a separate invariant class in v2.

### 2.4 What actually fixes each shape

The patcher's strategy table and the scanner's guard table both come from here. This is the remediation catalogue, ordered by how often it is the right answer.

**Atomic single statement update.** Push the decision into the `WHERE` clause so the database evaluates it under its own row lock.

```sql
UPDATE items SET stock = stock - :qty
WHERE id = :id AND stock >= :qty;
-- rowcount 0 means the guard rejected it
```

Preferred whenever the logic fits. No extra lock is held, no round trip is added, and there is nothing for a reviewer to object to.

**Pessimistic row lock.** `SELECT ... FOR UPDATE` inside the transaction, then compute, then write. Necessary when the decision involves logic that cannot be expressed in SQL. Lock rows in a deterministic order (primary key ascending) whenever more than one row is involved, or you have swapped a race for a deadlock. `FOR UPDATE SKIP LOCKED` is the queue variant: it is correct for worker claim patterns and wrong for the oversell case, since skipping a locked row silently changes the semantics.

**Unique constraint plus upsert.** For duplicate_insert, the constraint is the fix and the code change is secondary: `INSERT ... ON CONFLICT DO NOTHING` combined with a rowcount check. Without the index there is no atomic guard to lean on, so the patch must emit a migration alongside the code diff.

**Serializable isolation with a retry loop.** PostgreSQL's Serializable Snapshot Isolation detects read write dependency cycles and aborts one transaction. The abort arrives as SQLSTATE 40001, and PostgreSQL deliberately provides no automatic retry because it cannot know what logic produced the statements, so the application must retry the whole transaction including the decision logic. Deadlocks arrive as 40P01, unique violations as 23505, exclusion violations as 23P01, and each can be worth retrying in the same wrapper. Two consequences for RaceGuard:

* Serializable without a retry wrapper is not a guard. It converts silent data corruption into user visible 500s, which is better but not correct. The scanner must check for both, and report the isolation level alone as a partial guard at medium confidence.
* Retry loops must be bounded, use exponential backoff with jitter, and never swallow 40001 without retrying, because catching and continuing discards the transaction's writes silently.

**Advisory locks.** `pg_advisory_xact_lock(hashtext(:key))` serialises a critical section keyed by tenant, user, or resource when the invariant spans tables and no single row can carry the lock. Transaction scoped (`_xact_`) variants release on commit, which is what you want; session scoped ones leak if the connection is pooled.

**Optimistic concurrency with a version column.** Add `version integer`, and make the write `WHERE id = :id AND version = :seen_version`, incrementing on success. Rowcount 0 means someone else won; the caller retries or reports a conflict. This is the same mechanism the web exposes as ETag plus `If-Match`, where a stale validator yields 412 Precondition Failed and the client must re fetch before retrying. Google's API design guidance treats the etag field as the standard resource freshness validator for exactly this purpose.

**Idempotency keys.** Not a race fix on their own, but the correct answer to duplicate side effects from client retries. Stripe's model is the reference implementation: the client sends an `Idempotency-Key` header (a v4 UUID is the recommendation), the server stores the status code and response body of the first attempt under that key, and any replay returns the stored result rather than repeating the operation. Note two things when writing the RaceGuard rule for this: the header name is an industry convention rather than a ratified standard, since the IETF draft for it expired without becoming an RFC, and retention windows and parameter mismatch behaviour differ between providers, so RaceGuard should check for presence and atomic insertion of the key, not conformance to a spec that does not exist. The atomic part matters: the key must be inserted with a unique constraint before the work begins, otherwise two concurrent replays both miss the lookup and both proceed, which is a duplicate_insert race in the idempotency layer itself.

**Serialisation through a queue.** Push the mutation onto a per key ordered queue and process one at a time. Correct, and usually a larger architectural change than a linter should propose. RaceGuard mentions it in the explanation text and never generates it.

## 3. What production API platforms do, and why it matters here

The remediation catalogue above is not theoretical. Three patterns recur across large public APIs and they inform both what RaceGuard checks for and how RaceGuard itself behaves as a client.

**Idempotency on unsafe methods.** Stripe guarantees GET and DELETE idempotency inherently and makes POST idempotent through the key header, saving the first response and replaying it on repeat. Their client libraries retry automatically with increasing backoff and jitter.

**Layered rate limits.** GitHub runs a primary hourly quota alongside secondary limits that exist specifically to stop bursty and concurrent traffic: no more than 100 concurrent requests across REST and GraphQL, a points budget per minute where reads cost one point and writes cost five, a cap on content generating requests per minute, and a CPU time ceiling. Their guidance is to make mutating requests serially with at least a second between them, to honour `retry-after` when present, to stop until `x-ratelimit-reset` when `x-ratelimit-remaining` is zero, and to back off exponentially rather than continuing to hammer, since persistent violation can get an integration banned.

**Advertised limits.** The IETF httpapi working group has an active draft defining `RateLimit` and `RateLimit-Policy` response fields so servers can advertise quota and clients can self throttle. It is still a draft rather than an RFC, and the older `RateLimit-Limit`, `RateLimit-Remaining`, and `RateLimit-Reset` names remain the widely deployed de facto convention. So RaceGuard should read both spellings and, when generating a limiter, emit the older triple plus the newer combined field.

Three consequences for this project, each expanded later:

1. RaceGuard gains a hygiene module (section 11) that flags mutating endpoints with no idempotency key handling, no rate limit, and no optimistic concurrency validator, since those are the same class of defect one layer up.
2. RaceGuard's own request firing must be governed (section 12). A verification tool that fires 8 concurrent requests in a loop against an environment it does not own is indistinguishable from an attack.
3. When the target application has no rate limiter at all, RaceGuard can generate one, because "your endpoint has no limiter and no idempotency" is actionable in a way that "you have a race" sometimes is not.

## 4. Architecture

Six engine packages, four adapters, one contracts module. Nothing in the engine knows an adapter exists.

```
raceguard/
  contracts.py          # every object crossing a module boundary
  config.py             # raceguard.yaml load, validate, resolve env vars
  errors.py             # typed exceptions mapped to exit codes
  scanner/
    __init__.py         # scan(paths, options) -> ScanResult
    routes.py           # decorator resolution -> method + path template
    matchers.py         # read / write / guard / decision call signatures
    dataflow.py         # def-use linking within a handler scope
    rules/              # one module per pattern, each independently testable
    suppress.py         # inline "# raceguard: ignore[rule]" handling
  synth/
    __init__.py         # derive(finding, config, schema) -> Invariant
    schema.py           # reflection: columns, CHECK, UNIQUE, FK
    proposer.py         # model backed candidate generation
    critic.py           # second pass adversarial review of the candidate
    validate.py         # mechanical gate; nothing reaches the harness unvalidated
  harness/
    __init__.py         # verify(target) -> Verdict
    database.py         # container, template db, per trial db, seeding
    appboot.py          # container mode and inprocess mode
    fire.py             # arrival synchronisation strategies
    amplify.py          # gap widening instrumentation
    stats.py            # reproduction rate, Wilson interval, flake detection
    governor.py         # RaceGuard's own concurrency and rate governor
  hygiene/
    __init__.py         # idempotency, rate limit, optimistic concurrency checks
  patch/
    __init__.py         # propose(finding, invariant) -> Patch
    strategies.py       # decision table
    codemods/           # one libcst transformer per strategy
    migrations.py       # emitted Alembic operations
  report/
    text.py  json.py  sarif.py  html.py
  cli.py
  mcp_server.py
  action/               # GitHub Action entrypoint
examples/shop/          # deliberately vulnerable reference app
tests/corpus/
  vulnerable/  safe/  labels.yaml
```

Import discipline, enforced by a test that walks the module graph:

* `contracts.py` imports nothing from the project.
* Engine packages may import `contracts`, `config`, `errors`, and nothing else from each other, with one exception: `patch` may import `harness` to reverify.
* Only `cli.py`, `mcp_server.py`, and `action/` may import more than one engine package.

The reason is not aesthetics. Two adapters that both reimplement orchestration drift within a month, and the MCP server is the one you will demo.

## 5. Data contracts

Write this file first. Freeze it by end of week one. Changes are a team decision, not a pull request.

```python
# raceguard/contracts.py
from __future__ import annotations
from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, Field

Pattern = Literal[
    "lost_update", "oversell", "single_use",
    "duplicate_insert", "aggregate_limit",
]

InvariantKind = Literal[
    "non_negative", "conservation", "single_use",
    "duplicate_insert", "aggregate_limit",
]

class Confidence(str, Enum):
    high = "high"        # read, decide, write all linked in one scope, no guard
    medium = "medium"    # write or guard crosses a call boundary
    low = "low"          # something unresolvable is in the path

class CodeSpan(BaseModel):
    file: str
    start_line: int
    end_line: int
    snippet: str = ""

class Finding(BaseModel):
    id: str                       # sha1(file + qualname + pattern)[:12], stable across runs
    handler: str                  # "shop.routes.orders:create_order"
    route: Optional[str]          # "POST /orders"
    pattern: Pattern
    read_site: CodeSpan
    decision_site: Optional[CodeSpan]
    write_site: CodeSpan
    guard_found: Optional[str]    # name of the guard detected, if any
    guard_partial: Optional[str]  # e.g. "serializable isolation, no retry wrapper"
    confidence: Confidence
    explanation: str              # plain sentence; names what could not be resolved
    tables: list[str] = []        # tables touched, used to seed the synthesizer

class Invariant(BaseModel):
    kind: InvariantKind
    table: str
    column: Optional[str] = None
    predicate: str                # "items.stock >= 0", for humans
    check_sql: str                # MUST return zero rows when the invariant holds
    params: dict = {}
    source: Literal["config", "spec", "schema"]
    derivation: str               # the sentence, constraint, or config line it came from

class TrialRecord(BaseModel):
    index: int
    concurrency: int
    status_codes: list[int]
    arrival_spread_ms: float      # max minus min server receive time, when measurable
    final_state: dict
    violated: bool
    error: Optional[str] = None

class Verdict(BaseModel):
    finding_id: str
    target_id: str
    invariant: Invariant
    violated: bool
    violating_trials: int
    total_trials: int
    reproduction_rate: float
    ci95: tuple[float, float]     # Wilson interval, reported not asserted
    amplified: bool
    mode: Literal["container", "inprocess"]
    duration_s: float
    trials: list[TrialRecord]
    notes: list[str] = []         # e.g. "3 trials returned 500 before any write"

class Migration(BaseModel):
    kind: Literal["unique_index", "check_constraint", "version_column"]
    statement: str                # raw SQL
    alembic: str                  # equivalent op.* call

class Patch(BaseModel):
    finding_id: str
    strategy: Literal[
        "atomic_update", "select_for_update", "insert_on_conflict",
        "advisory_lock", "optimistic_version", "serializable_retry",
    ]
    diff: str                     # unified diff; never written without --apply
    rationale: str
    migration: Optional[Migration] = None
    reverified: Optional[Verdict] = None
    test_suite_passed: Optional[bool] = None

class HygieneFinding(BaseModel):
    id: str
    route: str
    rule: Literal[
        "missing_idempotency_key", "unsafe_idempotency_check",
        "missing_rate_limit", "missing_optimistic_concurrency",
        "unbounded_retry", "swallowed_serialization_failure",
    ]
    severity: Literal["high", "medium", "low"]
    explanation: str
    remediation_available: bool
```

Two invariants of the contract itself, both worth a comment in the file:

1. `check_sql` returns rows only on violation. One convention, checked in one place.
2. `violated=False` means "not reproduced under these conditions". Every renderer must phrase it that way. Add a unit test asserting the string "safe" never appears in report output.

## 6. Module 1: the scanner

### 6.1 Why libcst rather than ast

The standard `ast` module discards comments, whitespace, and parentheses, so anything built on it can analyse but not rewrite. LibCST parses modern Python into a lossless concrete syntax tree that keeps every formatting detail, and it ships a codemod framework with metadata providers and a declarative matcher system. Since the patcher must round trip user source without reformatting their file, and since running two different tree representations across two modules guarantees drift, the whole project uses libcst.

The pieces used:

* `libcst.matchers` for declarative call shape matching, which is far more maintainable than nested isinstance checks.
* `PositionProvider` for line spans on findings.
* `ScopeProvider` and `QualifiedNameProvider` for def use linking and for resolving `session.execute` back to a SQLAlchemy import rather than some unrelated object with an `execute` method.
* `libcst.codemod.VisitorBasedCodemodCommand` for the patch transformers, executed through `transform_module` so metadata resolution and error handling come for free.

### 6.2 Pass structure

Five passes per file. Bail out early on files with no route decorators, which keeps a 2000 file repo scan in the low seconds.

**Pass 1, handler discovery.** Collect functions whose decorators resolve to a Starlette route method. Handle the common indirections: `@app.post`, `@router.post`, a router assigned to a differently named variable, `APIRouter(prefix=...)` contributing to the path, and `include_router` in another module contributing a further prefix. Record the composed path template and method on the finding. When the prefix cannot be resolved statically, record the route as `None` rather than guessing, and let `raceguard init` ask.

Also record whether the handler is `async def` or `def`. This matters later: FastAPI runs `def` handlers in a thread pool and `async def` handlers on the event loop, and the two produce different interleaving behaviour under load.

**Pass 2, site classification.** A matcher table maps call shapes to roles. Keep it as data, not code, so adding a pattern is a table entry.

Read sites:

```python
READ_MATCHERS = [
    m.Call(func=m.Attribute(attr=m.Name("get"))),            # session.get(Item, id)
    m.Call(func=m.Attribute(attr=m.Name("scalar"))),         # await session.scalar(select(...))
    m.Call(func=m.Attribute(attr=m.Name("scalar_one"))),
    m.Call(func=m.Attribute(attr=m.Name("scalar_one_or_none"))),
    m.Call(func=m.Attribute(attr=m.Name("first"))),          # query(...).filter(...).first()
    m.Call(func=m.Attribute(attr=m.Name("one"))),
    m.Call(func=m.Attribute(attr=m.Name("one_or_none"))),
    m.Call(func=m.Attribute(attr=m.Name("execute"))),        # narrowed by argument inspection
]
```

`execute` is ambiguous, so inspect its argument: a `select(...)` call makes it a read, an `update(...)` or `insert(...)` makes it a write, and a `text("...")` gets a cheap SQL keyword sniff on the literal. Anything dynamic, such as `text(build_query())`, drops the finding to low confidence with the explanation "SQL constructed at runtime, not analysable".

Write sites: augmented assignment or plain assignment to an attribute of a tainted object, `session.add`, `session.add_all`, `session.execute(update(...))`, `session.execute(insert(...))`, `session.delete`, and raw `text()` containing UPDATE, INSERT, or DELETE.

Decision sites: any `If` test, `assert`, comparison, or boolean operation whose operands trace to a read site value. The decision is not required for a finding (a blind lost update has no decision), but its presence raises confidence and determines the pattern: a threshold comparison plus a decrement is `oversell`, a bare overwrite is `lost_update`.

**Pass 3, linking.** Build def use chains for local names within the handler scope. Taint propagates through:

* direct assignment, `item = await session.get(...)`
* attribute access on a tainted object, `item.stock`
* tuple unpacking from a tainted call
* membership in a list or dict that is later iterated

Do not attempt interprocedural analysis in v1. If the write happens inside a helper, record it at medium confidence with the call site as the write span. Interprocedural is the single largest source of both missed bugs and false positives, and it deserves its own release with its own evaluation.

**Pass 4, guard detection.** A window is only a finding if nothing on the path protects it. Recognised guards, each with a name that appears verbatim in `Finding.guard_found`:

| Guard | Detection | Verdict |
|---|---|---|
| `with_for_update` | `.with_for_update()` anywhere on the read chain | full guard |
| `atomic_expression` | update `values()` references the same column, e.g. `stock=Item.stock - qty` | full guard |
| `on_conflict` | `on_conflict_do_nothing` or `on_conflict_do_update` on an insert | full guard for duplicate_insert |
| `raw_for_update` | `FOR UPDATE` present in a `text()` literal | full guard |
| `advisory_lock` | `pg_advisory_xact_lock` or `pg_advisory_lock` executed before the read | full guard |
| `unique_constraint` | reflected unique index covers the natural key | full guard for duplicate_insert |
| `serializable` | engine or session `isolation_level` is SERIALIZABLE or REPEATABLE READ | partial unless a retry wrapper is found |
| `retry_wrapper` | a decorator or context manager whose body catches 40001 / `SerializationFailure` and re issues | completes the `serializable` guard |
| `version_check` | write `where` clause includes a version or updated_at equality against a read value | full guard |

Anything unrecognised on the path (a Redis lock, a custom context manager, a call into an unimported module) sets confidence to `low` and puts the unresolved symbol into `explanation`. Never claim a handler is guarded by something you could not identify, and never claim it is unguarded either.

**Pass 5, emission.** Deduplicate on `(file, qualname, pattern)`. Apply suppressions. Sort by confidence then file position so output is stable across runs, which matters for diffing SARIF uploads.

### 6.3 Suppression

Ship this in v1. The first false positive on a large repo decides whether the tool stays installed.

```python
item.stock -= qty  # raceguard: ignore[oversell] guarded by upstream advisory lock
```

Rules: the comment must name a rule id, a free text reason after the bracket is required (enforced), and `raceguard scan --show-suppressed` lists everything suppressed with its reason so suppressions stay auditable. Also support a `suppress:` block in config for whole files or handlers.

### 6.4 Confidence policy

Default CI gate fails on `high` only. `medium` appears in the report and in SARIF at note level. `low` is hidden unless `--min-confidence low`. This asymmetry is deliberate: a missed bug costs one bug, a false positive costs the tool.

### 6.5 Performance budget

Scan of a 100 file FastAPI service: under 3 seconds cold, under 1 second warm with the parse cache. Cache keyed by file content hash plus tool version, stored in `.raceguard/cache/`. The MCP `analyze_handler` tool is meant to run on every file save, so this budget is a hard requirement rather than a nice number.

## 7. Module 2: the invariant synthesizer

This module carries the project's claimed novelty, so its output must be auditable line by line. Three sources, tried in order, with the winner recorded in `Invariant.source` and the reason in `Invariant.derivation`.

### 7.1 Source 1: configuration

If `raceguard.yaml` states an invariant for the target, use it verbatim and stop. Human authored beats inferred, permanently.

### 7.2 Source 2: specification text

The target may carry a `spec:` line in natural language, or the handler's docstring may serve. The proposer sends that text plus the reflected schema for the tables named in the finding, and is constrained to emit a JSON object matching `Invariant` with `kind` drawn from the fixed enum.

The prompt contract, kept in `synth/proposer.py` as a versioned constant so evaluation results stay reproducible:

* Input: spec text, table definitions with column types and constraints, the code span of the decision site.
* Output: one JSON object, no prose.
* Rule stated in the prompt: only reference tables and columns present in the supplied schema.

### 7.3 Source 3: schema reflection

With no spec, reflect and infer:

* A `CHECK (stock >= 0)` yields non_negative directly, source `schema`, confidence full.
* A unique index on a natural key yields duplicate_insert or single_use.
* A non nullable numeric column that the flagged handler decrements yields non_negative at medium confidence, and `raceguard init` writes it into the config as a commented suggestion for the user to confirm.

### 7.4 The validation gate

Nothing reaches the harness unvalidated. `synth/validate.py` runs, in order:

1. Table and column exist in the reflected metadata. Reject otherwise.
2. `check_sql` parses and passes `EXPLAIN` against the empty schema in a scratch database. Reject on any SQL error.
3. `check_sql` returns zero rows against the seeded initial state. An invariant already violated before any request is fired is a bad invariant, not a discovered bug. Reject with a clear message naming the seed row.
4. `check_sql` returns at least one row against a deliberately corrupted state built by the validator (for non_negative: set the column to -1). This proves the check actually detects what it claims to detect. This step catches the subtle hallucination where the SQL is valid but tests the wrong thing.

A candidate failing any step is discarded. Do not retry in a loop until something passes; a loop that eventually accepts is a loop that accepts nonsense.

### 7.5 Proposer and critic

The proposer generates. A separate critic pass receives the candidate invariant, the schema, and the spec, and answers one question: name any state that satisfies this check but violates the stated requirement, or any state that fails this check but is actually acceptable. If the critic produces a concrete counterexample, the validator builds that state and tests it. This is a subagent pattern and is covered in section 19.2, including why the critic must be blind to the proposer's reasoning.

## 8. Module 3: the verification harness

The long pole. Start it in week one.

### 8.1 Database lifecycle

One PostgreSQL container per run via testcontainers, pinned by image digest rather than tag so a moved tag cannot silently change results. Inside it:

1. Create `rg_template`, apply the user's migration command against it once.
2. Per trial: `CREATE DATABASE rg_trial_{n} TEMPLATE rg_template`. Template copy is dramatically faster than rerunning migrations and, unlike truncation, it resets sequences and leaves constraints and extensions intact.
3. Seed the target's boundary state.
4. Run the trial.
5. Snapshot the final state of the involved tables into the `TrialRecord`, then drop the trial database.

Template copy requires no other session connected to the template, so the pool used for migration must be disposed before the first `CREATE DATABASE ... TEMPLATE` call. This is the single most common implementation bug in this module; write the test for it first.

Seeding philosophy: use the tightest state that can expose the invariant. For oversell, `stock = 1` with concurrency 2 is a far better signal than `stock = 100` with concurrency 50, because the final state distinguishes a violation cleanly rather than burying it in noise. `raceguard init` generates tight seeds automatically.

### 8.2 Booting the application

**container mode (default).** RaceGuard builds or pulls the app image, runs the configured start command with the database URL environment variable pointed at the trial database, waits for the health endpoint, and fires real HTTP. This preserves worker count, thread pool sizing, and process boundaries, which is the only way to reproduce races that depend on more than one process.

**inprocess mode.** Import the ASGI app, drive it through `httpx.ASGITransport`. Boot cost drops from roughly ten seconds to under one, which is what makes the MCP editor loop usable. It cannot reproduce multi process races and the report must label the mode on every verdict.

For `def` handlers, FastAPI dispatches to an anyio worker thread pool. If the pool is smaller than the configured concurrency, requests queue and the race cannot occur, producing a false negative. The harness must read the app's thread pool size where possible and warn when `concurrency` exceeds it.

### 8.3 Arrival synchronisation

Getting requests to arrive together is most of the reliability of this module. Three strategies, selected by config, each recorded on the verdict.

**barrier (default, simplest).** Pre warm the connection pool with a health request, then release all clients from an `asyncio.Barrier` and send.

```python
async def _fire(client, target, barrier):
    request = client.build_request(
        target.method, target.url, json=target.payload, headers=target.headers
    )
    await barrier.wait()
    return await client.send(request)

async def run_trial(target, n):
    barrier = asyncio.Barrier(n)
    limits = httpx.Limits(max_connections=n, max_keepalive_connections=n)
    async with httpx.AsyncClient(base_url=target.base_url, limits=limits) as client:
        await client.get(target.health_path)   # settle DNS, TCP, pool
        return await asyncio.gather(
            *(_fire(client, target, barrier) for _ in range(n)),
            return_exceptions=True,
        )
```

**last byte sync.** Open one connection per request, send everything except the final byte, pause briefly so all the bulk arrives, then send the withheld bytes together. Servers only begin processing a request they consider complete, so this collapses arrival spread even when the network delivered the bulk at different times. This is the HTTP/1.1 technique and it is what to implement second.

**single packet.** Over HTTP/2, issue every request on one connection while withholding a fragment from each (for a request with a body, all but the final byte; for one without, an empty DATA frame carrying END_STREAM), then complete 20 to 30 requests with one TCP packet. This removes network jitter entirely, which is why the technique exists. Practical notes from the published work: disable TCP_NODELAY so Nagle batches the final frames, ping first to warm the connection, and pause around 100 ms after the bulk send. Worth implementing for the paper and for remote targets; for a local container the barrier plus amplification usually suffices.

Even with perfect arrival, server side jitter (CPU contention, scheduler decisions) means two requests are often not enough. Firing 10 to 30 rather than 2 is what makes discovery reliable.

### 8.4 Gap amplification

Natural reproduction against a local PostgreSQL is unreliable, and an unreliable tool gets uninstalled. `harness/amplify.py` installs an instrumentation shim that sleeps a configured number of milliseconds after the first read of the target table within a request, widening the window deterministically.

Implementation: wrap the DBAPI cursor through a SQLAlchemy event listener registered only in the harness process, matching on the target table name, firing once per request via a context variable.

Run two phases per target and report both:

* **amplified** proves the read decide write window exists and is exploitable. This is the structural claim.
* **natural** measures how often it fires with no help. This is the operational claim.

Never merge them into one number. The distinction between "your code has a race window" and "your code loses money three times in a hundred" is the whole difference between an honest tool and an alarming one.

### 8.5 Trial protocol

For each trial: create db, seed, boot or reuse app, fire, collect responses, run `check_sql`, snapshot state, drop db. Record status codes and arrival spread where measurable.

Guard rails the checker needs:

* If every request returned 5xx, mark the trial `error` and exclude it from the rate denominator, with a note. An invariant that holds because the app crashed is not a pass.
* If no request returned 2xx across all trials, abort the target with a configuration error: the payload or auth is wrong, and reporting "not reproduced" would be a lie.
* If the final state equals the seed state in every trial, warn that no write occurred, which usually means the payload did not reach the handler path under test.

### 8.6 Statistics and honest reporting

`reproduction_rate = violating_trials / usable_trials`. Attach a Wilson score interval at 95 percent and print it. Report, never assert on it.

Asymmetric conclusions, stated in these words in the report footer:

* One violating trial out of any number is a proof of the defect.
* Zero violating trials out of T is evidence bounded by T, the seed, the concurrency, the mode, and the amplification setting. It is not a proof of safety.

Flake control: the nightly corpus job records reproduction rate per fixture over time. A fixture whose rate drifts is a harness bug, not a code change, and you want that visible as a trend rather than as a surprise in submission week.

### 8.7 Budgets and cleanup

Defaults: `trials: 20`, `concurrency: 8`, per trial timeout 30 s, per target budget 300 s, per run budget 30 min. All configurable, all enforced by a watchdog rather than by hope.

Register cleanup on SIGINT and SIGTERM and on unhandled exception: stop containers, drop trial databases, remove networks. Add `raceguard clean` for runs killed with SIGKILL, and label every container with `raceguard.run=<id>` so cleanup is a label filter rather than a name guess.

## 9. Module 4: the remediation engine

### 9.1 Strategy selection is a table, not a model call

| Condition | Strategy | Change emitted |
|---|---|---|
| Single row counter, threshold compared then decremented, decision expressible in SQL | `atomic_update` | `UPDATE ... SET c = c - :n WHERE pk = :id AND c >= :n`, rowcount check |
| Single row, decision logic too complex for SQL | `select_for_update` | `.with_for_update()` on the read, write kept in the same transaction |
| Create if absent, natural key identifiable | `insert_on_conflict` | `ON CONFLICT DO NOTHING` plus a unique index migration |
| Invariant spans rows or tables, no single row can carry the lock | `advisory_lock` | `pg_advisory_xact_lock(hashtext(:key))` before the read |
| Read exposed to a client that later writes back (edit flows) | `optimistic_version` | version column migration, `WHERE version = :seen`, 409 or 412 on rowcount 0 |
| Existing code already sets SERIALIZABLE but has no retry | `serializable_retry` | bounded retry wrapper catching 40001, 40P01, exponential backoff with jitter |

Prefer `atomic_update` whenever it fits. It adds no lock, no round trip, and no reviewer objection.

### 9.2 Worked transformation

```python
# before
item = await session.get(Item, item_id)
if item.stock < qty:
    raise HTTPException(409, "out of stock")
item.stock -= qty
await session.commit()

# after, strategy=atomic_update
result = await session.execute(
    update(Item)
    .where(Item.id == item_id, Item.stock >= qty)
    .values(stock=Item.stock - qty)
)
if result.rowcount == 0:
    raise HTTPException(409, "out of stock")
await session.commit()
```

```python
# after, strategy=select_for_update  (when the decision cannot move into SQL)
item = (
    await session.execute(
        select(Item).where(Item.id == item_id).with_for_update()
    )
).scalar_one_or_none()
if item is None or item.stock < qty:
    raise HTTPException(409, "out of stock")
item.stock -= qty
await session.commit()
```

### 9.3 Codemod rules

Each strategy is a `VisitorBasedCodemodCommand` matched to the finding's spans and executed with `transform_module`. Non negotiable behaviours:

* Add imports through libcst's `AddImportsVisitor` rather than string editing, and let the codemod framework clean up unused imports afterwards.
* Never reformat anything outside the changed statements. The CST preserves formatting; a diff touching 200 unrelated lines will not be merged.
* For multi row `select_for_update`, always emit `ORDER BY <primary key>` on the locked select. Locking rows in inconsistent order across handlers trades a race for a deadlock, and shipping that would be worse than shipping nothing.
* Every emitted retry loop is bounded, uses exponential backoff with jitter, and re raises after the cap. Never emit a bare `while True`.
* When a strategy needs a schema change, the `Migration` travels with the patch and the report states plainly that the code change alone is insufficient.

### 9.4 Apply protocol

`raceguard fix` prints a diff and exits. `--apply` runs this sequence:

1. Copy the repository working tree to a scratch directory (or require a clean git tree and work on a branch).
2. Apply the diff.
3. Run the migration against the ephemeral database.
4. Reverify the target: expect zero violations in T of T trials, amplified.
5. Run the user's `test_command`.
6. If both pass, write the change into the working tree and print the summary. If either fails, discard, and report which step failed with its output.

A patch that fixes a race and breaks four tests is worse than no patch, so step 5 is not optional.

## 10. Module 5: hygiene checks

Same scanner infrastructure, different rules, aimed one layer up from the SQL. These findings are cheap to produce and are often the thing a user acts on first.

### 10.1 missing_idempotency_key

Fires on a mutating route (POST, PATCH, DELETE) that creates a record or moves value and never reads an idempotency header. Explanation cites the pattern rather than a standard, because the `Idempotency-Key` header is an industry convention popularised by Stripe whose IETF draft expired without becoming an RFC, so semantics such as retention window and parameter mismatch behaviour vary by provider.

### 10.2 unsafe_idempotency_check

The subtle one, and the reason this module belongs in a race condition tool. Code that looks up the key, finds nothing, does the work, then stores the key is itself a check then act race: two concurrent replays both miss the lookup and both proceed. The correct shape inserts the key row first under a unique constraint and treats the constraint violation as "already in flight or done".

```python
# flagged: lookup then act
existing = await session.scalar(select(IdemKey).where(IdemKey.key == key))
if existing:
    return existing.response
result = await do_work()
session.add(IdemKey(key=key, response=result))

# accepted: claim first
try:
    await session.execute(insert(IdemKey).values(key=key, state="in_progress"))
    await session.commit()
except IntegrityError:
    return await replay(key)
```

### 10.3 missing_rate_limit

Fires when no limiter is detectable anywhere in the app: no limiter middleware, no dependency, no decorator, no reverse proxy config in the repo. Reported once per application rather than once per route, since 40 identical findings is noise.

Detection looks for the known Python options (slowapi, fastapi-limiter, FastAPI-Cap, asgi-ratelimit, a custom middleware whose name contains "limit" or "throttle") plus nginx or Envoy configuration in the repo.

### 10.4 missing_optimistic_concurrency

Fires on an update route that accepts a full resource body and writes it without any validator: no version column in the where clause, no `If-Match` header read, no `updated_at` equality. This is the classic lost update at the API layer, where the fix is a version or ETag validator and a 412 when the client's copy is stale.

### 10.5 unbounded_retry and swallowed_serialization_failure

Two code smells that show up around concurrency fixes people have already attempted: a retry loop with no cap or no backoff, and an `except` block that catches a serialization failure and continues without retrying. The second silently discards the transaction's writes, which is a data loss bug wearing the costume of a fix.

## 11. Generating a rate limiter when the app has none

If `missing_rate_limit` fires, `raceguard fix --rule missing_rate_limit` emits a working limiter rather than advice. Two variants.

### 11.1 In process limiter, no external dependency

Token bucket, since token bucket is what most public APIs use: normal traffic is bursty (batch jobs, app launches, retry storms), and a bucket accumulates credit while idle and permits a burst up to capacity without falsely limiting well behaved clients.

```python
# raceguard generated: token bucket limiter, single process
import time
from dataclasses import dataclass, field

@dataclass
class Bucket:
    tokens: float
    last: float

@dataclass
class TokenBucketLimiter:
    capacity: float            # burst size
    refill_per_second: float   # sustained rate
    _buckets: dict[str, Bucket] = field(default_factory=dict)

    def allow(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        now = time.monotonic()
        b = self._buckets.get(key)
        if b is None:
            b = self._buckets[key] = Bucket(self.capacity, now)
        b.tokens = min(self.capacity, b.tokens + (now - b.last) * self.refill_per_second)
        b.last = now
        if b.tokens >= cost:
            b.tokens -= cost
            return True, 0.0
        deficit = cost - b.tokens
        return False, deficit / self.refill_per_second   # seconds until allowed
```

The middleware wrapping it returns 429 with `Retry-After`, and advertises quota using both spellings: the older `RateLimit-Limit`, `RateLimit-Remaining`, and `RateLimit-Reset` triple that remains the de facto deployed convention, and the newer combined `RateLimit` and `RateLimit-Policy` fields from the active IETF draft.

The generated code carries an honest comment: single process state means N workers give N times the limit, so this is a starting point, not a distributed limiter.

### 11.2 Distributed limiter, Redis backed

When the repo already uses Redis, emit the shared variant, and do the whole decision inside one Lua script. This is the part that matters for a race condition tool: read tokens, compute refill, write tokens is itself a read modify write, and two API nodes doing it as separate commands will both allow a request that should have been rejected. A Lua EVAL executes atomically on the server and collapses the cycle into one operation, which is why every production guide reaches for it rather than WATCH/MULTI/EXEC.

```lua
-- KEYS[1] bucket key; ARGV: capacity, refill_per_sec, now_ms, cost
local capacity      = tonumber(ARGV[1])
local refill        = tonumber(ARGV[2])
local now           = tonumber(ARGV[3]) / 1000.0
local cost          = tonumber(ARGV[4])
local state         = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens        = tonumber(state[1]) or capacity
local ts            = tonumber(state[2]) or now
tokens = math.min(capacity, tokens + (now - ts) * refill)
local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], math.ceil(capacity / refill) + 1)
return {allowed, tostring(tokens)}
```

GCRA is the memory efficient alternative, storing a single theoretical arrival time instead of a token count and needing no refill bookkeeping, but it is harder to debug because you cannot ask it how many tokens remain. Token bucket with explicit state is the better default for generated code that a human will maintain.

Also emit, as commented suggestions rather than code: cost weighting so writes cost more than reads (GitHub charges roughly one point for reads and five for writes), and a separate concurrency cap alongside the rate cap, since GitHub's secondary limits cap concurrent requests at 100 independently of the hourly quota.

## 12. RaceGuard's own governor

RaceGuard is a load generator by construction. Left ungoverned it is indistinguishable from an attack, and the first time someone points it at a shared staging environment it will take that environment down and the tool will be banned internally. `harness/governor.py` is therefore mandatory, not a setting.

### 12.1 What the governor enforces

* **Global concurrency ceiling.** A semaphore capping in flight requests across all targets. Default 16. Trial concurrency draws from it, so verifying three targets in parallel cannot multiply into 24 connections.
* **Inter trial spacing.** A minimum pause between trials, defaulting to 250 ms, so a 20 trial run is a series of bursts rather than one sustained flood.
* **Per run request budget.** A hard ceiling on total requests. Exceeding it aborts with exit code 3 rather than continuing.
* **Token bucket on the outbound side.** The same algorithm from section 11, applied to RaceGuard's own requests, so sustained rate stays bounded while each individual trial still bursts (bursting is the point).
* **Backpressure.** On 429 or 503, stop the run, honour `Retry-After` if present, otherwise wait for `RateLimit-Reset` or `x-ratelimit-reset`, otherwise back off exponentially with jitter. Continuing to send while limited is what gets integrations banned.
* **Header awareness.** Parse both header spellings on every response and record the observed quota in the run artifact. If the target advertises a limit lower than the configured concurrency, refuse to run the target and say why.

### 12.2 Target ownership check

Before any run, RaceGuard verifies it is talking to a database and an app it created. If the resolved database URL is not the ephemeral container, refuse. `--allow-external-target` requires typing the host name to confirm, and even then the governor drops the concurrency ceiling to 4 and disables amplification. Someone will eventually point this at a URL they believe is staging.

### 12.3 MCP specific limits

The MCP surface has its own budget on top of the governor, because an agent in a loop will call a tool far more often than a human runs a CLI:

* `analyze_handler` is cheap and uncapped beyond the parse cache.
* `verify_handler` is capped per session (default 20 calls) and per target per hour (default 6). Exceeding returns a structured error explaining the cap rather than silently queuing.
* Every verify call defaults to `mode: inprocess` and `trials: 10` unless overridden, so the editor loop stays under about 15 seconds.
* The tool description states the cost in its first line, because that description is what the model reads when deciding whether to call it.

## 13. Configuration reference

```yaml
version: 1

app:
  module: "shop.main:app"                    # for inprocess mode
  start: "uvicorn shop.main:app --host 0.0.0.0 --port 8000 --workers 4"
  dockerfile: "Dockerfile"                   # optional, for container mode
  health_path: "/health"
  base_url: "http://localhost:8000"
  database_url_env: "DATABASE_URL"
  migrate: "alembic upgrade head"
  test_command: "pytest -q"
  thread_pool_size: 40                       # informational, used to warn on concurrency

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
    concurrency: 2                           # tight seed, tight concurrency
    trials: 30

  - id: transfer
    route: "POST /wallet/transfer"
    payload: { from_id: 1, to_id: 2, amount: 50 }
    seed:
      accounts:
        - { id: 1, balance: 50 }
        - { id: 2, balance: 0 }
    invariant:
      kind: conservation
      table: accounts
      column: balance
      predicate: "SUM(balance) = 50"

  - id: checkout_race
    group:                                    # multi endpoint shape
      - { route: "POST /cart/apply-coupon", payload: { code: "SAVE10" } }
      - { route: "POST /cart/checkout", payload: {} }
    invariant:
      kind: single_use
      table: coupon_redemptions

hygiene:
  enabled: true
  rules: ["missing_idempotency_key", "unsafe_idempotency_check", "missing_rate_limit"]

suppress:
  - handler: "shop/routes/admin.py::bulk_import"
    rule: "oversell"
    reason: "single admin caller, serialised upstream by the import queue"
```

Nobody hand writes this. `raceguard init` generates it: routes from decorators, payloads from the endpoint's Pydantic request model, seed rows from the SQLAlchemy model with boundary values filled in, invariants from schema constraints. The user edits auth and specifics. That generator is the difference between a tool people try once and a tool people keep.

## 14. Adapters

### 14.1 CLI

```
raceguard init [path]                  # generate raceguard.yaml from a scan
raceguard scan [path] [--format text|json|sarif] [--min-confidence high|medium|low]
                      [--show-suppressed] [--hygiene]
raceguard verify [--target ID ...] [--trials N] [--concurrency N]
                 [--mode container|inprocess] [--no-amplify]
raceguard fix [--target ID] [--rule RULE] [--apply]
raceguard report [--run latest|ID] [--format text|html|json]
raceguard clean                        # remove orphaned containers and databases
raceguard doctor                       # check docker, image pull, ports, permissions
```

Exit codes, because CI systems distinguish them and conflating them makes a broken runner look like a security finding:

| Code | Meaning |
|---|---|
| 0 | Completed, nothing above the configured gate |
| 1 | Findings or violations above the gate |
| 2 | Configuration or usage error |
| 3 | Infrastructure failure (Docker unavailable, image pull failed, budget exceeded) |
| 4 | Interrupted |

Every run writes `.raceguard/runs/<timestamp>-<id>/` containing the resolved config, findings, invariants with derivations, verdicts, trial records, final state dumps, diffs, and a log. Bug reports become "attach the run folder". Redact `Authorization` and anything matching a secret pattern before writing, since those folders get pasted into issues.

### 14.2 MCP server

The protocol moved recently, so pin deliberately. The Python SDK's v2 line is the current stable release and supports the 2026-07-28 specification along with every earlier revision; in v2 the `FastMCP` class is now `MCPServer`, while the decorator API is unchanged. Because `pip install mcp` now resolves to 2.x, a project not ready to migrate must pin an upper bound such as `mcp>=1.28,<2`. The 2026 revision is stateless: no initialize handshake, no protocol level session, which means an MCP server can sit behind a plain round robin load balancer. Structured tool output arrived in the 2025-06-18 revision and is generated automatically from return types that have a JSON object representation, so returning a Pydantic model gives agents machine readable results without extra work.

```python
# raceguard/mcp_server.py   (SDK v2)
from mcp.server import MCPServer
from raceguard.scanner import scan
from raceguard.harness import verify_target
from raceguard.patch import propose
from raceguard.contracts import ScanResult, Verdict, Patch

mcp = MCPServer("raceguard")

@mcp.tool()
def analyze_handler(path: str) -> ScanResult:
    """Cheap static scan for read-modify-write windows. Milliseconds. Safe on every save."""
    return scan([path])

@mcp.tool()
def verify_handler(target_id: str, trials: int = 10) -> Verdict:
    """EXPENSIVE (about 15s): boots an ephemeral Postgres and fires concurrent
    requests. Call only for a target already flagged by analyze_handler."""
    return verify_target(target_id, trials=trials, mode="inprocess")

@mcp.tool()
def suggest_fix(finding_id: str) -> Patch:
    """Returns an unapplied unified diff plus rationale. Never writes to disk."""
    return propose(finding_id)
```

Logging note: MCP level logging is deprecated in the 2026 revision, with stderr for stdio transports and OpenTelemetry as the documented path for observability. Do not build the run log on `ctx.info`.

### 14.3 GitHub Action and SARIF

Scan output maps to SARIF 2.1.0, which is the version GitHub code scanning accepts from third party tools, and uploads through `github/codeql-action/upload-sarif`. Constraints worth knowing before designing the report: the file must be under 10 MB, private and internal repositories need Code Security enabled, and if more than one results file is uploaded for the same tool in one workflow each needs a distinct `category` or a unique `runAutomationDetails.id`.

Mapping:

* `Finding.pattern` becomes a `reportingDescriptor` rule with a stable id such as `raceguard/oversell`, carrying `shortDescription`, `fullDescription`, and a help URI to the docs page for that pattern, since GitHub surfaces those at the top of an alert.
* `Finding.confidence` maps to level: high to `error`, medium to `warning`, low to `note`.
* `read_site` and `write_site` become the primary location and a related location, so the alert shows both ends of the window.
* Verdicts, when present, are appended to the alert message with the reproduction rate and the mode.

Workflow shape: scan on every push, verify only the targets whose handler files appear in the diff, since verifying everything on every push will outlast the rest of the pipeline.

### 14.4 Pre commit hook

Scan only, staged files only, high confidence only, no Docker required. The verify step never belongs in a pre commit hook; a 30 second wait on commit gets the hook removed.

## 15. Security and sandboxing

RaceGuard executes other people's code and other people's SQL. Treat that as a design constraint from the first commit rather than a hardening pass at the end.

Container posture for the application under test: no network egress except to the trial database on an internal Docker network, all capabilities dropped, read only root filesystem with a writable tmpfs, memory and CPU quotas, no host mounts other than the source directory mounted read only, and a wall clock kill.

Process posture for `inprocess` mode: this imports user code into the RaceGuard process, so it is strictly less safe. Print that once per run. Anyone running the tool against an untrusted repository should use container mode, and the MCP server should default to container mode when it detects it is running outside the user's own project directory.

Secret handling: environment variable interpolation in config resolves at load time and the resolved values never enter run artifacts. Response bodies are truncated and header values redacted by pattern before serialisation.

Supply chain: pin the PostgreSQL image by digest, pin every Python dependency with a lock file, and enable Dependabot. A tool that people run with Docker access is a tool worth attacking.

## 16. Observability

Structured JSON logs to stderr with a run id on every line, a human readable renderer for the terminal, and OpenTelemetry spans around container start, migration, each trial, and each check query. The trial span carries concurrency, mode, amplification, arrival spread, and violation as attributes, which turns "why is this flaky" from an argument into a query.

Metrics worth emitting even locally: container boot seconds, migration seconds, per trial seconds, requests fired, violations found, and governor pauses. The last one tells you whether the governor is actually engaging or whether it is decorative.

## 17. Test strategy and quality gates

### 17.1 The corpus

Build `tests/corpus/` before the scanner, not after. Roughly 40 handlers, labelled in `labels.yaml`, split into `vulnerable/` and `safe/`, covering all five invariant classes and every guard in the table from section 6.2. Include the awkward cases on purpose:

* A guard living in a helper function called by the handler.
* An `async def` handler using a sync session, and a `def` handler using an async engine through a bridge.
* A correct `atomic_update` that a naive matcher will flag anyway.
* SERIALIZABLE set on the engine with a retry wrapper, and the same without one.
* A raw `text()` query with `FOR UPDATE` embedded.
* Dynamic SQL built by string concatenation, which must land at low confidence rather than being flagged or cleared.
* An idempotency implementation with the lookup then act race, alongside a correct claim first version.

### 17.2 Gates

These are also the evaluation table for the paper.

| Metric | Gate |
|---|---|
| Scanner recall on `vulnerable/` at high plus medium | at least 90 percent |
| Scanner false positives on `safe/` at high | under 5 percent |
| Harness reproduction, amplified | 100 percent of corpus vulnerabilities within 20 trials |
| Harness false alarms on `safe/` | zero across three consecutive nightly runs |
| Patch correctness | every proposed patch passes reverification and the corpus test suite |
| Scan performance | 100 files under 3 seconds cold |
| Verify performance, inprocess | single target, 10 trials, under 15 seconds |

### 17.3 Test types

Unit tests per matcher and per codemod with golden input and output files. Property tests on the invariant validator: generate random schemas, assert that generated `check_sql` returns zero rows on a satisfying state and at least one row on a violating one. Integration tests running the full pipeline against `examples/shop`. A nightly job over the whole corpus recording reproduction rates over time.

## 18. Packaging and distribution

Publish as `raceguard` on PyPI, built with `uv`, Python 3.11+, with Docker as the only external requirement. Ship a container image too, for CI users who would rather not install Python tooling. Semantic versioning, with the promise that `raceguard.yaml` schema version 1 keeps working across all 1.x releases.

The README opens with a sixty second path: install, `cd examples/shop`, `raceguard scan .`, `raceguard verify --target purchase`, watch a real oversell reproduce against a real PostgreSQL. That sample app carries the demo, the integration tests, and half the documentation, so keep it small and keep it correct.

Documentation pages that must exist before the first release: config reference, what reproduction rate does and does not mean, the recognised guard list (people will want to know why their handler was flagged), suppression, the remediation catalogue with the reasoning behind each strategy, and a security page describing the sandbox.

## 19. Where subagents belong

Two separate uses. Keep them separate in your heads and in the repo, because they fail differently.

### 19.1 Subagents in the development workflow

Subagents are isolated Claude Code instances with their own context window, tool permissions, and model, spawned by the main session; the lead sees only each one's final summary, which keeps the main transcript focused on orchestration. Custom definitions live in `.claude/agents/` as YAML plus a Markdown body, are version controlled and shareable across your three person team, and unique names matter because two definitions sharing a name in one scope means one is silently discarded. They are a heavy mechanism: each costs a full context window and round trip overhead, so use one when the savings exceed the cost and skip it when a skill or a plain tool call would do. Three to five concurrent is the practical sweet spot for most work.

For a project split across four engine modules, the natural roster:

```yaml
# .claude/agents/corpus-labeler.md
---
name: corpus-labeler
description: Writes labelled fixtures for tests/corpus. Never touches raceguard/.
tools: Read, Write, Grep
model: sonnet
---
Write one handler per fixture file into tests/corpus/{vulnerable,safe}/.
Each fixture gets an entry in labels.yaml naming: pattern, expected confidence,
guard present (if any), and one sentence on why it is or is not vulnerable.
Never edit files under raceguard/. Never write a fixture whose label you cannot
justify from the code alone.
```

```yaml
# .claude/agents/matcher-dev.md
---
name: matcher-dev
description: Adds and refines scanner matchers against the corpus. Read-only outside scanner/.
tools: Read, Write, Bash, Grep
model: opus
---
Work only inside raceguard/scanner/. After every change run
`pytest tests/test_scanner.py -q` and report precision and recall on the corpus.
Never change a label in tests/corpus/labels.yaml to make a test pass; if a label
looks wrong, stop and report it.
```

The last line of that definition is the important one. A subagent with write access to both the code and the labels will eventually fix the metric instead of the bug.

Other roles worth defining: `codemod-dev` scoped to `raceguard/patch/`, `harness-dev` scoped to `raceguard/harness/` with Docker permissions, `docs-writer` with no write access to source, and `reviewer` which is read only and runs before every merge.

Where fan out actually pays here:

* Corpus construction. Four subagents writing fixtures for four invariant classes in parallel, then one merge pass. Read heavy, independent, no shared file.
* Matcher precision sweeps. One subagent per guard variant, each reporting which fixtures it breaks. Parallel execution is safe for read only analysis; anything that writes should be serialised through the lead.
* Cross module integration review before each milestone: one read only reviewer per module, summaries merged by the lead.

Where fan out does not pay: anything that edits `contracts.py`, anything that touches the same file, and the harness, where the work is one long sequential debugging thread against real containers rather than parallel exploration.

Note that background subagents inherit permissions approved at launch and auto deny anything not pre approved, which is what you want for a Docker heavy project. If you need to disable backgrounding entirely, for CI or a locked down machine, set `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`.

### 19.2 Subagents inside RaceGuard

Only two places, and both are wrapped in mechanical verification. The rule for the whole product: no model output is ever trusted; it is a proposal that must survive a deterministic check.

**Invariant proposer and critic.** The proposer emits a candidate `Invariant` constrained to the schema. The critic receives the spec, the schema, and the candidate, and is asked one question only: name a database state that satisfies this check but violates the requirement, or one that fails this check but is acceptable. The critic never sees the proposer's reasoning, because a critic primed with the proposer's justification agrees with it. If the critic names a state, the validator constructs it and tests the SQL against it. Every candidate then passes the four step gate from section 7.4 regardless of what either agent said.

**Patch explainer.** The strategy is chosen by the decision table and the transformation is done by libcst. A model only writes the `rationale` string a human reads in the diff. It has no ability to alter the code being emitted. Keeping generation mechanical is what makes the patch reviewable, and reviewability is the only reason anyone accepts an automated fix.

Explicitly not agent driven: strategy selection, code transformation, verdict computation, and anything touching the reproduction rate. Those are the parts a reviewer will probe, and "a model decided" is not an answer that survives.

## 20. Build plan

Ten weeks, three people. Owner names are roles, not seniority.

**Week 1, everyone.** `contracts.py`, repo skeleton with import discipline test, `examples/shop`, corpus fixtures and labels, CI running lint, types, and tests. Nobody starts a module before contracts are agreed. Subagent fan out is worth it here for corpus construction.

**Weeks 2 to 4, parallel.**

*A: scanner.* Route resolution, matcher table, data flow linking, guard detection, suppression, SARIF. Target by end of week 4: corpus precision and recall gates met.

*B: harness.* Week 2, one pattern end to end against a real container. Week 3, template database resets and stable trial timing, plus the governor. Week 4, remaining invariant classes and the two application boot modes. This is the long pole and it must start on day one of week 2.

*C: config, synthesis, CLI.* Schema reflection, the four step validation gate, proposer and critic, `raceguard init`, CLI skeleton with exit codes and run artifacts. C also produces stub outputs for A and B so neither blocks.

**Week 5.** Integration. First real end to end run on `examples/shop`. Budget the whole week for unglamorous mismatches between what the scanner emits and what the harness needs; there will be more than you expect.

**Weeks 6 and 7.** Remediation engine: strategy table, one codemod per strategy, migration emission, apply protocol with reverification and revert. Report layer: text, JSON, SARIF, HTML.

**Week 8.** Hygiene module, generated rate limiter, MCP server on the pinned SDK, GitHub Action, pre commit hook.

**Week 9.** Sandbox posture, resource limits, cleanup paths, `doctor` command, error messages, packaging, docs, first PyPI release.

**Week 10.** Full corpus evaluation, the numbers table, freeze and tag.

IPR sequencing: the copyright registration and any provisional application go in before the exhibition or any public demo, since public disclosure in India damages the novelty position. Keep the repository private until the filing date, then flip it public and let the release be the demo.

## 21. Risk register

| Risk | Signal it is happening | Mitigation |
|---|---|---|
| Harness slips past week 4 | Trial timing still jumping around at end of week 3 | Started week 1; if week 4 ends without stable rates, cut to one boot mode and one sync strategy |
| False positives sink adoption | Corpus precision under 95 percent at high confidence | Default gate on high only, suppressions shipped in v1, explanation string names the unresolved symbol |
| Synthesizer produces a plausible wrong invariant | A verdict that nobody can explain | Four step validation gate, critic pass, derivation printed on every verdict |
| Reproduction rate misread as production probability | Someone quotes "8 percent chance of overselling" | Two phase reporting, footer language, no single blended number anywhere |
| Patch introduces a deadlock | Reverification hangs rather than fails | Deterministic lock ordering in generated code, lock timeout in the harness, deadlock detection as an explicit failure mode |
| Tool used against a live environment | Someone files an issue about staging going down | Ownership check, explicit override with typed confirmation, governor caps |
| Postgres only limits reach | Early users all ask for MySQL | Say so in the README on day one; keep the database layer behind an interface so v2 is an adapter |

## 22. Open questions to settle in week 1

1. Does `raceguard verify` support the multi endpoint `group` shape in v1, or does the config merely reserve the key? Reserving is the safer answer and costs nothing later.
2. Does `--apply` require a clean git tree, or does it work on a scratch copy? Clean tree is simpler and more predictable; scratch copy is friendlier.
3. Is the HTML report worth building, or does SARIF plus terminal output cover every consumer? Defer until week 7 and decide on evidence.
4. Which model backs the proposer and critic, and is the tool usable with no model configured at all? It must be: schema derived invariants alone should give a working experience, with spec derivation as an enhancement.

## 23. References

Concurrency control and PostgreSQL semantics
* PostgreSQL transaction isolation: https://www.postgresql.org/docs/current/transaction-iso.html
* PostgreSQL serialization failure handling (40001, 40P01, 23505, 23P01): https://www.postgresql.org/docs/current/mvcc-serialization-failure-handling.html

Web race condition testing technique
* Smashing the state machine, PortSwigger Research: https://portswigger.net/research/smashing-the-state-machine
* The single packet attack: https://portswigger.net/research/the-single-packet-attack-making-remote-race-conditions-local
* Web Security Academy, race conditions: https://portswigger.net/web-security/race-conditions

API design practice
* Stripe idempotency: https://docs.stripe.com/error-low-level and https://stripe.com/blog/idempotency
* GitHub REST rate limits, primary and secondary: https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
* GitHub REST best practices: https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api
* RateLimit header fields for HTTP (IETF draft): https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/
* AIP-154, resource freshness validation with etag: https://google.aip.dev/154

Rate limiting implementation
* Redis rate limiting patterns with atomic Lua: https://redis.io/tutorials/howtos/ratelimiting/
* FastAPI-Cap, algorithms including GCRA: https://github.com/devbijay/FastAPI-Cap

Tooling
* LibCST codemods and metadata: https://libcst.readthedocs.io/en/latest/codemods.html
* MCP Python SDK v2: https://github.com/modelcontextprotocol/python-sdk
* MCP 2026-07-28 SDK betas and stateless protocol: https://blog.modelcontextprotocol.io/posts/sdk-betas-2026-07-28/
* Testcontainers for Python: https://testcontainers.com/guides/getting-started-with-testcontainers-for-python/
* SARIF support for code scanning: https://docs.github.com/en/code-security/reference/code-scanning/sarif-files/sarif-support
* Uploading a SARIF file to GitHub: https://docs.github.com/en/code-security/code-scanning/integrating-with-code-scanning/uploading-a-sarif-file-to-github
