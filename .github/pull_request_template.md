## What and why

<!-- What changes, and the reason it is needed. -->

## Quality gates

<!-- Paste the output of `python tests/eval_corpus.py`. -->

```
recall (high+medium)     ____%   gate >= 90%
false positives (high)   ____%   gate <= 5%
```

## Checklist

- [ ] `ruff check raceguard tests` passes
- [ ] `mypy raceguard` passes
- [ ] `pytest tests -q` passes (including `-m docker` if the harness changed)
- [ ] No corpus label was changed to make a test pass
- [ ] Nothing in the output claims a handler is safe, secure, or verified
- [ ] New behaviour has a test that would fail without the change
- [ ] `CHANGELOG.md` updated under Unreleased
