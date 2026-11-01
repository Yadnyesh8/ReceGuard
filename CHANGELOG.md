# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`raceguard.yaml` schema version 1 is guaranteed to keep working across every
1.x release.

## [Unreleased]

### Added

- `raceguard ci`: one CI engine for GitHub Actions, GitLab CI and any other
  system, with changed-only gating on pull requests, a fingerprint baseline,
  a verification wall-clock budget, GitLab Code Quality output, inline
  annotations and a sticky PR/MR comment. The GitHub Action and a new GitLab
  template both call it.
- `raceguard baseline write|check` and `Finding.fingerprint`, a line-independent
  key that also feeds SARIF `primaryLocationLineHash`.
- `raceguard mcp serve|install`: the MCP server moved to `raceguard.mcp` with a
  streamable HTTP transport, bearer auth, a path sandbox, per-client budgets, a
  verify concurrency gate, `/healthz` `/readyz` `/metrics` `/version`, resources,
  prompts, tool annotations and drain-on-shutdown. `raceguard.mcp_server` stays
  as a shim.

### Changed

- The scanner reports `lost_update` only when the stored value depends on the
  row, and `duplicate_insert` only when the insert runs on the path where the
  row was absent. Renames, deletes and child inserts no longer fail the default
  gate. Rule output is keyed on the tool version, so caches refresh on upgrade.
- The natural phase always runs every trial. It used to stop on a hit, which
  inflated the rate it exists to report, and it now takes longer.
- With `--changed-only`, a diff that touches nothing under the scanned path
  gates nothing, instead of gating on every file. A failed diff still does.
- `database.extensions` are created in the template, so trials inherit them.
- The `mcp` extra needs SDK 2.1 or later. The SDK v1 path crashed at startup.

### Security

- `--allow-external-target` now compares the host you type. Any value was
  accepted, and `allow_external_target: true` alone was enough. A burst at an
  external target is now capped by the ceiling as documented.
- The app under test is published on loopback only.
- `get_run` and `raceguard://runs` no longer follow symlinks out of the sandbox.
- `verify_handler` over HTTP now requires the `verify` token scope. Scopes were
  parsed but never checked, so a read-only token could run project code. Add
  `:verify` to any token that should keep verifying.

### Fixed

- Amplification sleeps after the read, not before it, on every SQLAlchemy engine
  including async ones, and a run where it never fired is reported as natural
  timing. It used to attach only to modules named `shop`, `app` or `src` and
  claim an amplified run regardless.
- `inprocess` mode no longer reimports a project-local `.venv`, which failed
  every trial, and reports an app crash as a 500 rather than as infrastructure.
- Seeding a table with no `id` column no longer rolls back every seed row.
- `check_sql` may contain `%`.
- `raceguard verify --no-amplify` no longer crashes.
- `raceguard scan` takes several paths, which the pre-commit hook has always
  passed. A missing path, or a bad `--fail-on`, `--min-confidence` or `--format`,
  is exit 2 rather than a clean pass or a traceback reported as exit 1.
- `raceguard fix --apply` exits 1 when a patch is rejected, and reverifies against
  the matched target's own invariant.
- `raceguard ci` gates correctly on a subdirectory of a repository: paths from
  `git diff` never matched findings, so nothing in the diff was gated.
- `raceguard.yaml` suppressions apply in `ci`, `fix`, `baseline` and the MCP
  server, not only `scan`. `ci` also honours the `hygiene` block.
- A run started on Docker-in-Docker or a remote daemon is no longer refused as
  an external target.
- `run_budget_s` and advertised rate limits are enforced, a governor stop ends
  the run instead of erroring every remaining trial, and backpressure pauses
  once per trial rather than once per response.
- `single_packet` over plain http runs `last_byte` and says so. It used to send
  the burst sequentially and report the race as not reproduced.
- `start` honours shell quoting, and the amplification shim no longer replaces
  the image's own `PYTHONPATH`.
- `raceguard verify` reports the targets that finished when a later one fails,
  rather than discarding them.
- Three of the reference app's five races could not be verified:
  `/coupons/redeem` returned 500 on every request, `/teams/{id}/members` hung on
  an engine shared across event loops, and the `transfer` target's identical
  requests could never break its own check.
- `--allow-anonymous` no longer returns 401 when a reverse proxy forwards its
  `Authorization` header.
- The per-client verify cap is a rolling window (`RACEGUARD_MCP_SESSION_WINDOW`,
  one day). It used to lock a hosted token out until the server restarted.
- `/readyz` and `doctor` no longer require Docker when `RACEGUARD_DATABASE_URL`
  supplies the database.
- The MCP server honours `PORT`, as hosts such as Render expect. `RACEGUARD_MCP_PORT` still
  wins.


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

[Unreleased]: https://github.com/Yadnyesh8/ReceGuard/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Yadnyesh8/ReceGuard/releases/tag/v0.1.0
