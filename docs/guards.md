# The recognised guard list

People whose handler was flagged want to know why, and people whose handler was
not flagged want to know what cleared it. Both answers are here.

A read-decide-write window is only reported when nothing on the path protects
it. These are the guards RaceGuard recognises, each named verbatim in
`Finding.guard_found`.

| Guard | How it is detected | Verdict |
|---|---|---|
| `with_for_update` | `.with_for_update()` anywhere on the read chain | full guard |
| `atomic_expression` | `values()` references the same column, e.g. `stock=Item.stock - qty` | full guard |
| `on_conflict` | `on_conflict_do_nothing` or `on_conflict_do_update` on an insert | full guard for `duplicate_insert` |
| `raw_for_update` | `FOR UPDATE` inside a `text()` literal | full guard |
| `advisory_lock` | `pg_advisory_xact_lock` or `pg_advisory_lock` before the read | full guard |
| `unique_constraint` | a reflected unique index covering the natural key | full guard for `duplicate_insert` |
| `version_check` | the write's `where` compares a version or `updated_at` against a read value | full guard |
| `serializable` | engine or session `isolation_level` is SERIALIZABLE or REPEATABLE READ | **partial** unless a retry wrapper is found |
| `retry_wrapper` | a decorator or context manager catching 40001 and re-issuing | completes the `serializable` guard |

## Why SERIALIZABLE alone is only partial

PostgreSQL's Serializable Snapshot Isolation detects read-write dependency
cycles and aborts one transaction with SQLSTATE 40001. It deliberately provides
**no automatic retry**, because it cannot know what logic produced the
statements, so the application must re-issue the whole transaction including
the decision.

Setting the isolation level and stopping there converts silent data corruption
into user-visible 500s. That is better, and it is not correct. RaceGuard
reports it as a partial guard and caps the finding at medium confidence.

A retry wrapper completes the guard only if it is bounded, backs off, and
actually re-issues. An `except` that catches 40001 and continues silently
discards that transaction's writes, which is a data-loss bug wearing the
costume of a fix; that is its own hygiene finding,
`swallowed_serialization_failure`.

## What happens with something unrecognised

If something on the path cannot be identified — a Redis lock, a custom context
manager, a call into a module that was not imported — the finding drops to
**low** confidence and the unresolved symbol is named in the explanation.

RaceGuard never claims a handler is guarded by something it could not identify,
and it never claims it is unguarded either.

## Confidence policy

| Confidence | Meaning | Default CI gate |
|---|---|---|
| high | read, decision and write all linked in one scope, no guard | fails the build |
| medium | the link crosses a call boundary, or a partial guard is present | reported, SARIF note level |
| low | something on the path is unresolvable | hidden unless `--min-confidence low` |

The asymmetry is deliberate: a missed bug costs one bug, a false positive costs
the tool.
