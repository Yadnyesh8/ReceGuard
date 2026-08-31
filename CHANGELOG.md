# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`raceguard.yaml` schema version 1 is guaranteed to keep working across every
1.x release.

## [Unreleased]

## [0.1.0] - 2026-08-31

First release.

### Added

- **Scanner.** Five-pass static analysis over FastAPI and Starlette handlers
  using libcst: route resolution through `APIRouter` prefixes and
  `include_router`, a data-driven matcher table, def-use linking with one-hop
  helper inlining, guard detection, and inline plus config suppression. Six
  rules: `oversell`, `lost_update`, `single_use`, `duplicate_insert`,
  `aggregate_limit`, and a low-confidence rule for handlers whose SQL is built
  at runtime.
- **Invariant synthesizer.** Three sources tried in order (config, spec text,
  schema reflection) with the winner and its derivation recorded on every
  verdict. A blind critic pass, and a four-step mechanical validation gate that
  no candidate skips.
- **Verification harness.** Ephemeral PostgreSQL via testcontainers, template
  database per run and a fresh database per trial, three arrival
  synchronisation strategies (barrier, last-byte, single-packet), deterministic
  gap amplification, and Wilson score intervals.
- **Governor.** Global concurrency ceiling, inter-trial spacing, per-run
  request budget, outbound token bucket, backpressure on 429 and 503 honouring
  both rate-limit header spellings, and a target-ownership check.
- **Remediation.** Six libcst codemods behind a decision table, with migrations
  emitted where the code change alone is insufficient, and an apply protocol
  that reverifies and runs your test suite before keeping anything.
- **Hygiene rules.** Idempotency (including the check-then-act race inside an
  idempotency implementation itself), rate limiting, optimistic concurrency,
  unbounded retries, and swallowed serialization failures. Generates a working
  token-bucket limiter, in-process or Redis-backed.
- **Adapters.** CLI with distinct exit codes and run artifacts, MCP server on
  SDK v2 with per-session and per-target budgets, GitHub Action emitting SARIF
  2.1.0, and a pre-commit hook.
- **Reference application** in `examples/shop` with five reproducible races and
  one correctly guarded endpoint.
- **Corpus** of 40 labelled fixtures and the evaluation harness that gates on
  them.

### Notes

- The `group:` config key for multi-endpoint races is supported for firing;
  richer cross-endpoint invariants are planned for a later release.
- `mode: inprocess` cannot reproduce races that depend on more than one
  process. Every verdict records the mode it ran in.

[Unreleased]: https://github.com/raceguard/raceguard/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/raceguard/raceguard/releases/tag/v0.1.0
