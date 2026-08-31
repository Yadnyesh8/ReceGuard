# The remediation catalogue

Strategy selection is a **table, not a model call**. The finding's shape picks
the row; the row picks the transformation. A language model only writes the
rationale prose a human reads in the diff, and it cannot alter the code emitted.

That is deliberate. Keeping generation mechanical is what makes the patch
reviewable, and reviewability is the only reason anyone accepts an automated
fix.

| Condition | Strategy |
|---|---|
| Single-row counter, threshold compared then decremented, decision expressible in SQL | `atomic_update` |
| Single row, decision logic too complex for SQL | `select_for_update` |
| Create-if-absent, natural key identifiable | `insert_on_conflict` |
| Invariant spans rows or tables, no single row can carry the lock | `advisory_lock` |
| Read handed to a client that later writes back | `optimistic_version` |
| SERIALIZABLE already set but no retry wrapper | `serializable_retry` |

## atomic_update, preferred whenever it fits

```python
result = await session.execute(
    update(Item)
    .where(Item.id == item_id, Item.stock >= qty)
    .values(stock=Item.stock - qty)
)
if result.rowcount == 0:
    raise HTTPException(409, "out of stock")
```

The database evaluates the comparison under its own row lock. No extra lock is
held, no round trip is added, and there is nothing for a reviewer to object to.
`rowcount == 0` now carries the rejection.

## select_for_update, when the decision cannot move into SQL

A predicate the database has never heard of — a pricing engine, a regex, a call
to another service — cannot go in a `WHERE` clause. Pushing it there would
change behaviour, not fix a race. Lock the row instead and keep the decision in
Python, inside the same transaction.

**Lock rows in a deterministic order** (primary key ascending) whenever more
than one row is involved, or you have swapped a race for a deadlock. The
generated code emits `ORDER BY` on the primary key for multi-row locks, and a
deadlock is the worse bug because it fails under exactly the load the fix was
meant to survive.

`FOR UPDATE SKIP LOCKED` is the queue variant. It is correct for worker-claim
patterns and **wrong** for the oversell case, since skipping a locked row
silently changes the semantics.

## insert_on_conflict, where the constraint is the fix

```python
result = await session.execute(
    insert(User).values(email=email)
    .on_conflict_do_nothing(index_elements=["email"])
    .returning(User)
)
```

The code change is secondary. Without a unique index there is nothing to
conflict against, so the patch **always ships a migration** and the report says
plainly that the diff alone is insufficient.

## advisory_lock, when no row can carry it

```python
await session.execute(
    select(func.pg_advisory_xact_lock(func.hashtext("teams:" + str(team_id))))
)
```

The `_xact_` variant is deliberate: a transaction-scoped lock releases on
COMMIT, which is what you want behind a connection pool. A session-scoped lock
leaks when the connection is returned still holding it, and the leak only shows
up under load.

Every handler enforcing the same invariant must take the **same key**, or the
lock serialises nothing.

## optimistic_version

Add a `version` column, make the write conditional on the version last read,
and increment on success. `rowcount == 0` means somebody else won: the caller
retries or gets a 409. This is the same mechanism the web exposes as ETag plus
`If-Match`, where a stale validator yields 412 and the client must re-fetch.
Ships with a migration.

## serializable_retry

PostgreSQL provides no automatic retry for SQLSTATE 40001, because it cannot
know what logic produced the statements. The application must re-issue the
**whole transaction including the decision**; retrying only the write would
replay a decision made against a snapshot that no longer exists.

Every generated loop is bounded, backs off exponentially with jitter, and
re-raises after the cap. A bare `while True` is never emitted.

## Serialisation through a queue

Pushing the mutation onto a per-key ordered queue is correct, and it is a
larger architectural change than a linter should propose. RaceGuard mentions it
in explanation text and never generates it.

## The apply protocol

`raceguard fix` prints a diff and exits. `--apply` runs:

1. require a clean git tree,
2. apply the diff,
3. run the migration against the ephemeral database,
4. reverify: expect zero violations in T of T trials, amplified,
5. run your `test_command`,
6. keep the change only if both passed; otherwise discard and report which step
   failed, with its output.

Step 5 is not optional. A patch that fixes a race and breaks four tests is
worse than no patch.

## When nothing is proposed

If the handler's shape does not match what a strategy rewrites, or a name the
new code would need cannot be recovered from the handler, RaceGuard emits **no
diff** and says why. A patch that does not compile, or one that references an
invented local, is worse than an obviously missing one.
