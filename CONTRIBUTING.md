# Contributing

## Getting set up

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev,harness,mcp]"
pre-commit install
```

Docker is needed for the integration tests. Everything else runs without it.

## Running the checks

```bash
ruff check raceguard tests          # lint
ruff format --check raceguard       # formatting
mypy raceguard                      # types
pytest tests -q -m "not docker"     # fast suite
pytest tests -q -m docker           # full pipeline against real PostgreSQL
python tests/eval_corpus.py         # the quality gates
```

## The rules that are not negotiable

These are enforced by tests. If a change trips one, the change is wrong, not
the test.

1. **The tool never claims safety.** It reports what it found statically and
   what it reproduced dynamically, under stated conditions, and never says a
   handler is safe, secure, or verified. `tests/test_report_language.py`
   enforces this across every renderer.

2. **Never change a corpus label to make a test pass.** If a label in
   `tests/corpus/labels.yaml` looks wrong, say so in the pull request and stop.
   Adjusting the label instead of the matcher fixes the metric rather than the
   bug, and the resulting numbers are worse than no numbers because they look
   earned.

3. **The import graph holds.** `contracts.py` imports nothing from the project.
   Engine packages share only `contracts`, `config`, `errors`, and `db`.
   Orchestration lives in the adapters. `tests/test_import_discipline.py`
   checks this.

4. **No model output is trusted.** Anything a language model produces is a
   proposal that must survive a deterministic check. Strategy selection, code
   transformation, and verdict computation are mechanical and stay that way.

5. **A patch that does not compile is worse than no patch.** When a handler's
   shape does not match what a strategy rewrites, emit nothing and say why.

## Quality gates

A pull request must not regress these:

| Metric | Gate |
|---|---|
| Scanner recall on `vulnerable/` at high + medium | ≥ 90% |
| Scanner false positives on `safe/` at high | < 5% |
| Every proposed patch applies, parses, and defines every name it uses | 100% |
| Scan of 100 files, cold | < 3 s |

`python tests/eval_corpus.py` prints the first two and exits non-zero when
either fails.

## Adding a pattern

1. Write fixtures first, in `tests/corpus/{vulnerable,safe}/`, with labels you
   can justify from the code alone.
2. Add the rule module under `raceguard/scanner/rules/`.
3. Add the remediation strategy and its codemod.
4. Add the SARIF rule descriptor so GitHub renders a useful alert.
5. Re-run the corpus evaluation and report the before and after numbers in the
   pull request.

## Commit style

Present tense, imperative, explaining *why* where it is not obvious. The
codebase leans on comments that explain reasoning rather than restating code;
please match that.
