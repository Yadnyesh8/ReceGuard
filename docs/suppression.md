# Suppression

The first false positive on a large repository decides whether the tool stays
installed, so suppression shipped in v1 rather than waiting for someone to ask.

## Inline

```python
item.stock -= qty  # raceguard: ignore[oversell] guarded by an upstream advisory lock
```

Three rules, all enforced rather than merely documented:

1. **The comment must name a rule id.** `ignore[]` is rejected with a message.
   `ignore[*]` suppresses everything at that location, and is honest about it.
2. **A free-text reason is required.** `# raceguard: ignore[oversell]` with no
   reason is refused and the finding is still reported. A suppression with no
   reason is a silent decision, and silent decisions are what make a
   suppression file worthless a year later.
3. **It is rule-scoped.** `ignore[oversell]` does not silence a
   `duplicate_insert` in the same handler.

Several rules can share one comment:

```python
# raceguard: ignore[oversell,lost_update] both are serialised by the import queue
```

A comment counts anywhere inside the window it is talking about, from the line
above the read to the line below the write, because people put the comment
where it reads naturally rather than on whichever line the scanner anchors to.

## In the config

For a whole handler or a whole file:

```yaml
suppress:
  - handler: "shop/routes/admin.py::bulk_import"
    rule: oversell
    reason: "single admin caller, serialised upstream by the import queue"

  - file: "shop/legacy.py"
    rule: "*"
    reason: "scheduled for deletion in Q3; tracked in PLAT-4412"
```

`reason` is required here too.

## Keeping them auditable

```bash
raceguard scan . --show-suppressed
```

lists everything suppressed with its reason and location. A suppression that
nobody can see is a suppression that becomes permanent.

## When not to suppress

If the reason you are about to write is "I think this is fine", verify it
instead:

```bash
raceguard verify --target <id>
```

A verdict is worth more than a comment. Suppress when the guard is real but
lives somewhere RaceGuard cannot see — an upstream queue, a proxy, a service
boundary — and say exactly where in the reason.
