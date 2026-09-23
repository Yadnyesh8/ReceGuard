# CI tool and hostable MCP framework: specification

Status: implemented in this tree. This document is the contract the code is
tested against; where the two disagree the tests win and this file is wrong.

## 1. Why

Two adapters exist today and both are thin:

* `action/entrypoint.py` is GitHub-only, reads `INPUT_*` from the environment,
  and installs from a PyPI package that is not published yet. GitLab, Buildkite,
  CircleCI, Jenkins and Azure users have nothing.
* `mcp_server.py` speaks stdio only, has no auth, no health endpoint, a single
  process-wide budget shared by every client, and lets an agent point
  `analyze_handler` at any path on the host.

Both problems have the same shape: orchestration that lives in the adapter
instead of in one engine every adapter calls. Section 4 of the design already
names this rule ("two adapters that both reimplement orchestration drift within
a month"). This spec applies it.

## 2. Goals and non-goals

Goals

1. One CI engine, `raceguard ci`, that every CI system calls with the same
   semantics: scan, optionally verify what changed, gate, emit every output
   format the host can consume, and exit with a code the host can act on.
2. Provider adapters that differ ONLY in how inputs arrive and where outputs go:
   GitHub Actions, GitLab CI, and a generic path for everything else.
3. Adoptable on a legacy codebase: gate on changed files by default on pull
   requests, and support a committed baseline so existing findings are reported
   without blocking the build.
4. An MCP server that a host can run: stdio for desktop clients, streamable
   HTTP for hosted deployment, bearer auth, health and readiness endpoints,
   per-client budgets, a project sandbox, and a clean shutdown that leaves no
   containers behind.
5. Everything above covered by tests that need no network and no Docker, plus
   the existing Docker suite for the parts that boot a database.

Non-goals

* A hosted SaaS control plane. The server is host-ready; running a fleet of
  them is somebody else's product.
* OAuth authorization server. The SDK supports it; this spec ships static
  bearer tokens and leaves the `TokenVerifier` seam for an operator to plug in
  JWT validation.
* Writing to the user's tree from MCP. `suggest_fix` returns a diff. There is
  no `apply_fix` tool and there will not be one: an agent applying a patch it
  cannot reverify is the one outcome the apply protocol exists to prevent.
* SSE transport. Deprecated in the protocol; streamable HTTP replaces it.

## 3. CI engine

### 3.1 Command

```
raceguard ci [PATH]
  --provider      auto | github | gitlab | generic        (default auto)
  --base REF      diff base; auto-detected per provider
  --changed-only / --all-files                            (default: changed-only on PR, all on push)
  --fail-on       high | medium | low | never             (default high)
  --baseline PATH .raceguard/baseline.json                (optional)
  --verify / --no-verify                                  (default no)
  --verify-required                                       fail (not skip) when Docker is missing
  --budget-seconds N                                      total wall clock cap for verification
  --hygiene / --no-hygiene                                (default hygiene)
  --sarif PATH  --json PATH  --codequality PATH  --summary PATH
  --annotations / --no-annotations                        provider-native inline annotations
  --comment / --no-comment                                sticky PR/MR comment
```

The command is the only entry point. `python -m raceguard.action.entrypoint`
maps `INPUT_*` to these flags and calls it. The GitLab template calls it
directly. Everyone else calls it directly.

### 3.2 Provider detection

| Provider | Detected by | Base ref | Changed-only default |
|---|---|---|---|
| github | `GITHUB_ACTIONS=true` | `GITHUB_BASE_REF` on `pull_request`, else `GITHUB_EVENT_BEFORE` / none | PR: yes; push: no |
| gitlab | `GITLAB_CI=true` | `CI_MERGE_REQUEST_DIFF_BASE_SHA`, else `CI_COMMIT_BEFORE_SHA` | MR: yes; push: no |
| generic | anything else, including `CI=true` | `--base` only | no |

`--base` always wins. Changed files are `git diff --name-only BASE...HEAD`,
falling back to `git diff --name-only BASE` when the merge base is unavailable
(shallow clones). A failed diff yields an empty set and `--changed-only`
degrades to `--all-files` with a visible notice, never a silent pass.

### 3.3 Gating

A finding is gated (fails the build) when ALL hold:

1. `confidence.rank() >= fail_on.rank()`,
2. it is not suppressed,
3. its fingerprint is not in the baseline,
4. under `--changed-only`, its `read_site.file` is in the changed set.

Verification verdicts gate when `violated` is true, regardless of the above.
Every finding is still written to SARIF and JSON; gating decides the exit code,
not the report. Exit codes: 0 clean, 1 gated findings or violations, 2 config
error, 3 infrastructure error. These are the existing `EXIT_*` constants.

### 3.4 Fingerprints and the baseline

`Finding.id` is unique per physical handler (it includes the read line), which
is right for dedupe and for SARIF but wrong for a baseline: editing a line above
the handler changes the id and resurrects the finding. The baseline therefore
matches on `Finding.fingerprint`:

```
sha1(file | handler qualname | pattern | route)[:16]
```

Two same-named handlers differ by route, so the fingerprint stays distinct; a
line shift changes neither component, so it stays stable. SARIF
`partialFingerprints.primaryLocationLineHash` carries the same value so GitHub
code scanning tracks the alert across pushes.

```
raceguard baseline write [PATH] [-o .raceguard/baseline.json]
raceguard baseline check [PATH]     # exit 1 on findings not in the baseline
```

The file is JSON: `{"version": 1, "generated": ISO8601, "fingerprints": [...]}`.
A stale entry (a fingerprint no longer produced) is reported so baselines shrink
rather than rot.

### 3.5 Verification in CI

Only targets whose handler file is in the changed set are verified (existing
behaviour). Additions:

* `--budget-seconds` caps the whole verification phase. When the budget is hit
  the remaining targets are recorded as skipped with that reason, the exit code
  reflects what DID run, and the summary says which targets were not verified.
  A check that silently outlasts the pipeline is the check that gets removed.
* Docker absent: skip with a warning and a summary line. `--verify-required`
  turns that into exit 3.
* Verification never runs on a provider without a base ref unless
  `--verify --all-files` is explicit, because "verify everything on every push"
  is the shape that gets the tool switched off.

### 3.6 Outputs

| Format | Consumer | Notes |
|---|---|---|
| SARIF 2.1.0 | GitHub code scanning, GitLab SAST, Azure | existing builder; `runAutomationDetails.id` is the category |
| JSON | scripts | `{"findings": [...], "verdicts": [...], "gated": [...], "skipped": [...]}` |
| GitLab Code Quality | GitLab MR widget | `[{"description", "check_name", "fingerprint", "severity", "location": {"path", "lines": {"begin"}}}]` |
| Markdown summary | `GITHUB_STEP_SUMMARY`, MR comment, stdout | one builder, three destinations |
| Annotations | GitHub workflow commands; GitLab via Code Quality | `::warning file=,line=,title=::` per gated finding; `::notice` for the rest, capped at 50 |
| Sticky comment | GitHub PR, GitLab MR | marker `<!-- raceguard:sticky -->`; update in place, never a second comment |

The sticky comment uses `GITHUB_TOKEN` / `GITLAB_TOKEN` (falling back to
`CI_JOB_TOKEN`) over `httpx`. No token means no comment and a notice, not a
failure. The comment body is the Markdown summary.

### 3.7 GitHub Action

`action.yml` stays composite. Changes:

* Installs from the checked-out action (`${{ github.action_path }}`) when
  `version` is empty, so it works before a PyPI release; a non-empty `version`
  installs that release.
* New inputs: `changed-only`, `baseline`, `budget-seconds`, `comment`,
  `annotations`, `verify-required`, `codequality-file`, `json-file`, `version`.
* `permissions` documented: `security-events: write` for SARIF upload,
  `pull-requests: write` for comments, `contents: read`.
* Outputs add `gated` and `exit-code`.

### 3.8 GitLab CI

`ci/gitlab/raceguard.gitlab-ci.yml` is an includable template defining a
`raceguard` job that runs `raceguard ci --provider gitlab`, publishes
`gl-code-quality-report.json` as `artifacts:reports:codequality` and
`raceguard.sarif` as `artifacts:reports:sast`, and honours the same variables
(`RACEGUARD_FAIL_ON`, `RACEGUARD_VERIFY`, ...). Docker-in-Docker is documented
for verify; scan needs nothing.

### 3.9 Generic CI

`docs/ci.md` shows Buildkite, CircleCI, Jenkins and Azure invocations. All are
`raceguard ci --base "$SOME_VAR"` plus an artifact upload step. Nothing
provider-specific lives in the engine for them.

## 4. MCP framework

### 4.1 Layout

`raceguard/mcp/` replaces `raceguard/mcp_server.py`, which becomes a
compatibility shim re-exporting `mcp` and `main`.

```
mcp/
  __init__.py     build_server(settings) -> MCPServer
  settings.py     MCPSettings from env and flags
  sandbox.py      project root anchoring and path jail
  budget.py       per-client budgets and the verify semaphore
  auth.py         StaticTokenVerifier (RACEGUARD_MCP_TOKENS)
  tools.py        analyze_handler, verify_handler, suggest_fix, check_hygiene,
                  list_targets, get_finding, get_run, doctor
  resources.py    raceguard://config, runs/{id}, findings/{id}, docs/{name}
  prompts.py      triage_finding, explain_verdict
  http.py         Starlette app: /mcp, /healthz, /readyz, /metrics, /version
  server.py       transport selection and lifecycle (stdio | http)
```

### 4.2 Settings

All from env with flag overrides (`raceguard mcp serve`):

| Setting | Env | Default |
|---|---|---|
| transport | `RACEGUARD_MCP_TRANSPORT` | stdio |
| host, port | `RACEGUARD_MCP_HOST`, `RACEGUARD_MCP_PORT` | 127.0.0.1, 8765 |
| project_root | `RACEGUARD_MCP_ROOT` | cwd |
| allow_any_path | `RACEGUARD_MCP_ALLOW_ANY_PATH` | false |
| tokens | `RACEGUARD_MCP_TOKENS` | empty |
| allow_anonymous | `RACEGUARD_MCP_ALLOW_ANONYMOUS` | false |
| allowed_hosts | `RACEGUARD_MCP_ALLOWED_HOSTS` | host:port |
| session_cap, target_cap | `RACEGUARD_MCP_SESSION_CAP`, `_TARGET_CAP` | 20, 6 |
| max_concurrent_verifies | `RACEGUARD_MCP_MAX_VERIFIES` | 1 |
| verify_enabled | `RACEGUARD_MCP_VERIFY` | true |
| log_format | `RACEGUARD_LOG_FORMAT` | text (json for hosted) |

Startup refuses `--transport http` with no tokens unless `allow_anonymous` is
set explicitly. Anonymous HTTP on a non-loopback host is refused outright; the
flag exists for a reverse proxy that terminates auth, and the log says so.

### 4.3 Sandbox

Every tool that takes a path resolves it against `project_root` and rejects
anything that escapes it (after `resolve()`, so symlinks cannot walk out).
`allow_any_path` disables the jail for a local stdio session that genuinely
spans several checkouts; it is ignored on HTTP. The refusal is a structured
`ToolError`, not an exception.

### 4.4 Budgets and concurrency

The existing per-session and per-target caps stay but are keyed by client
identity: the bearer token's subject over HTTP, a single `"stdio"` key
otherwise. A process-wide `asyncio.Semaphore(max_concurrent_verifies)` bounds
how many databases can be booting at once regardless of how many clients are
connected. A verify that would block on the semaphore returns a structured
"busy, retry after N seconds" result rather than queueing invisibly.

### 4.5 Tools

| Tool | Cost | Annotations |
|---|---|---|
| analyze_handler(path) | cheap | readOnly, idempotent |
| check_hygiene(path) | cheap | readOnly, idempotent |
| list_targets(config_path?) | cheap | readOnly, idempotent |
| get_finding(finding_id, path) | cheap | readOnly, idempotent |
| suggest_fix(finding_id, path) | cheap | readOnly (never writes) |
| verify_handler(target_id, trials?, config_path?) | expensive | not idempotent, not destructive |
| get_run(run_id, path) | cheap | readOnly |
| doctor() | cheap | readOnly |

The first line of every description still states the cost. Annotations let
hosts render a confirmation prompt on `verify_handler` and none on the rest.

### 4.6 Resources and prompts

Resources give a host reference material without a tool call:

* `raceguard://config` : the redacted resolved config, or an explanation that
  none was found.
* `raceguard://runs/{run_id}` : the stored run artifact.
* `raceguard://findings/{finding_id}` : one finding from the latest scan.
* `raceguard://docs/{name}` : the Markdown under `docs/` (guards,
  remediation, reproduction-rate, ...), so an agent can cite the actual
  wording rather than paraphrase it.

Prompts are templates a host can surface as slash commands:

* `triage_finding(finding_id)` : the finding, the invariant, and the question
  "is this reachable under concurrent load, and what is the tightest seed".
* `explain_verdict(target_id)` : the verdict with the asymmetry sentence, so
  an agent explaining "not reproduced" cannot drop the bound.

### 4.7 HTTP surface

Streamable HTTP at `/mcp` via the SDK's Starlette app, mounted inside a
Starlette app that also serves:

* `GET /healthz` : 200 always once the process is up (liveness).
* `GET /readyz` : 200 when the scanner imports and, if verify is enabled,
  Docker answers `info` within 5 s; 503 with a JSON reason otherwise.
* `GET /metrics` : Prometheus text format: tool call counts by name and
  outcome, verify durations histogram, in-flight verifies, budget refusals.
* `GET /version` : `{"raceguard": ..., "mcp_sdk": ..., "protocol": ...}`.

Auth is a bearer-token middleware in front of `/mcp` only; the health and
metrics endpoints are unauthenticated so a load balancer can probe them, and
`/metrics` can be disabled with `RACEGUARD_MCP_METRICS=false`. DNS rebinding
protection uses the SDK's `TransportSecuritySettings` with `allowed_hosts`.

### 4.8 Lifecycle

`SIGTERM` and `SIGINT`: stop accepting verifies (the semaphore is closed),
wait up to 30 s for in-flight verifies, run harness cleanup for any live run
context, then exit. Containers left behind are the bug this exists to prevent.

### 4.9 Host installation

`raceguard mcp install <host>` prints (or with `--write` writes) the config
snippet for `claude-desktop`, `claude-code`, `cursor`, and `vscode`, using the
resolved path to the current interpreter's `raceguard` executable so the host
does not depend on `PATH`. `raceguard mcp serve` starts the server.
`mcp/server.json` is the registry manifest. The container image gains a
documented second entrypoint: `raceguard mcp serve --transport http --host
0.0.0.0`.

## 5. Security model

* Stdio: the host is the trust boundary. The sandbox is still on by default
  because an agent that decides to "scan the whole disk" is a real failure
  mode, not a hypothetical.
* HTTP: bearer tokens, loopback by default, DNS rebinding protection, no
  anonymous non-loopback listen. `/mcp` never returns a stack trace; every
  refusal is a `ToolError` with a hint.
* Nothing under `raceguard/mcp/` writes to the project tree. Run artifacts go
  under `.raceguard/runs/` exactly as the CLI's do.
* Tokens are compared in constant time and never logged. `RACEGUARD_MCP_TOKENS`
  format: `token` or `token:scope1+scope2`, entries comma separated. Scopes are
  recorded on the `AccessToken` for a downstream verifier; the shipped tools do
  not gate on them.

## 6. Test plan

`tests/test_ci.py` (no Docker):

* provider detection for each env shape, `--base` precedence,
* changed-file computation against a real temporary git repo,
* gating matrix: confidence, suppression, baseline, changed-only,
* baseline write/check round trip and stale-entry reporting,
* fingerprint stability under a line shift and distinctness across routes,
* Code Quality JSON shape and severity mapping,
* Markdown summary contents, annotation formatting and the 50 cap,
* sticky comment: creates when absent, updates when the marker exists (httpx
  `MockTransport`), no token means no request,
* exit codes for every branch, including verify skipped without Docker and
  `--verify-required`.

`tests/test_mcp.py` (no Docker):

* sandbox: inside, outside, symlink escape, `allow_any_path` on stdio and
  ignored on HTTP,
* budgets keyed per client; semaphore returns busy rather than blocking,
* token verifier: valid, invalid, scopes parsed, anonymous refused on HTTP,
* HTTP app via `httpx.ASGITransport`: `/healthz` 200, `/readyz` shape,
  `/version`, `/metrics` content type, `/mcp` 401 without a token and not 401
  with one,
* every tool callable directly returns a Pydantic model or `ToolError`,
* resources and prompts registered with the expected URIs and names,
* `install` renders valid JSON for each host,
* startup refuses anonymous non-loopback HTTP.

Import discipline: `ci` and `mcp` join `ADAPTERS`.

## 7. Rollout

1. Engine and tests. 2. Adapters (action, GitLab template, shim). 3. Docs
(`docs/ci.md`, `docs/mcp.md`, README pointers). 4. This file marked
implemented. The existing `self-scan` CI job switches to `raceguard ci` so the
repository dogfoods the new path on every push.
