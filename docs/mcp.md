# The MCP server

RaceGuard speaks MCP so an agent can find a race, prove it, and get a patch
without leaving the editor. Two transports, one server.

## For an editor (stdio)

```
pip install "raceguard[harness,mcp]"
raceguard mcp install claude-code --write     # or claude-desktop, cursor, vscode
```

`install` prints the host's config snippet, or writes it with `--write`,
using the absolute path of this interpreter's `raceguard` so the host does not
depend on PATH. Claude Code users can also run
`claude mcp add raceguard -- $(which raceguard) mcp serve`.

Every path an agent passes is resolved inside the project root (the working
directory, or `RACEGUARD_MCP_ROOT`) and anything that escapes, symlinks
included, is refused with a hint. `--allow-any-path` lifts that for a local
session that genuinely spans several checkouts.

## For hosting (streamable HTTP)

```
export RACEGUARD_MCP_TOKENS="s3cret:verify,readonly-token"
raceguard mcp serve --transport http --host 0.0.0.0 --port 8765 --project-root /srv/app
```

or, from the container image:

```
docker run --rm -p 8765:8765 \
  -e RACEGUARD_MCP_TRANSPORT=http -e RACEGUARD_MCP_HOST=0.0.0.0 \
  -e RACEGUARD_MCP_TOKENS=s3cret -e RACEGUARD_MCP_ROOT=/src \
  -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD:/src" \
  ghcr.io/raceguard/raceguard mcp serve
```

The endpoints:

| Path | Auth | Purpose |
|---|---|---|
| `POST /mcp` | bearer | the protocol |
| `GET /healthz` | none | liveness |
| `GET /readyz` | none | 200 when the scanner imports and, if verify is on, Docker answers; 503 with reasons otherwise |
| `GET /metrics` | none | Prometheus text: tool calls by outcome, verify durations, in-flight, budget refusals |
| `GET /version` | none | tool and SDK versions |

The server refuses to start over HTTP with no tokens unless
`--allow-anonymous` is set, and refuses anonymous on any non-loopback host
outright; that flag exists for a reverse proxy that terminates auth on
`127.0.0.1`. Tokens are `token` or `token:scope+scope`, comma separated,
compared in constant time, and never logged; the budget is keyed by a hash of
the token so one client cannot spend another's.

**DNS rebinding protection and the Host header.** On a loopback bind the
protection is on and strict, because that is the case the attack targets: a
malicious page resolving a name to `127.0.0.1` to reach a server only you can
reach. On a wildcard bind (`0.0.0.0`, the container case) it cannot work
unattended, because the bind address is never the Host a client sends and a
published port differs from the internal one (`:8765` inside, `:18810`
outside). Enforcing it there rejects every real request, so it is disabled
with a warning and bearer auth carries the security. Set
`RACEGUARD_MCP_ALLOWED_HOSTS=mcp.example.com` (comma separated, `:*` wildcards
allowed) to turn enforcement back on once you know the external name.

`SIGTERM` closes the budget, drains in-flight verifications for up to
`RACEGUARD_MCP_SHUTDOWN_GRACE` seconds (default 30) so their containers are
torn down, and exits.

## Tools

| Tool | Cost | Notes |
|---|---|---|
| `analyze_handler(path)` | milliseconds | static findings; suspicions, not results |
| `check_hygiene(path)` | milliseconds | idempotency, rate limit, optimistic concurrency rules |
| `list_targets(config_path?)` | milliseconds | the ids `verify_handler` takes |
| `get_finding(finding_id, path)` | milliseconds | one finding by id |
| `suggest_fix(finding_id, path)` | milliseconds | unapplied diff plus rationale; never writes |
| `get_run(run_id, path)` | milliseconds | a recorded CLI run's artifacts |
| `doctor()` | seconds | can this server scan and verify |
| `verify_handler(target_id, trials?, config_path?)` | about 15 s | boots PostgreSQL; budgeted |

Every description opens with the cost, because that line is what the model
reads when deciding whether to call it. `verify_handler` is capped per client
session (`RACEGUARD_MCP_SESSION_CAP`, 20) and per target per hour
(`RACEGUARD_MCP_TARGET_CAP`, 6), and only `RACEGUARD_MCP_MAX_VERIFIES` (1) run
at once server-wide. Exceeding any of these returns a structured refusal with
a retry hint; nothing queues invisibly. Tool annotations mark everything but
`verify_handler` read-only so a host can skip its confirmation prompt.

There is no `apply_fix` tool. A patch is applied by `raceguard fix --apply`
from a terminal, which reverifies and runs your tests before keeping it.

## Resources and prompts

* `raceguard://config`: the resolved config, secrets redacted.
* `raceguard://runs/{run_id}`, `raceguard://findings/{finding_id}`.
* `raceguard://docs/{name}`: this documentation, so an agent can quote the
  wording of the reproduction-rate asymmetry instead of paraphrasing it into a
  claim the tool never made.
* Prompts `triage_finding(finding_id)` and `explain_verdict(target_id)`, which
  carry the bound with them: "not reproduced" is evidence under stated
  conditions, never "safe".

## Settings

| Env | Default | Meaning |
|---|---|---|
| `RACEGUARD_MCP_TRANSPORT` | `stdio` | `stdio` or `http` |
| `RACEGUARD_MCP_HOST`, `_PORT` | `127.0.0.1`, `8765` | HTTP bind |
| `RACEGUARD_MCP_ROOT` | cwd | sandbox root |
| `RACEGUARD_MCP_ALLOW_ANY_PATH` | `false` | disable the sandbox (stdio only) |
| `RACEGUARD_MCP_TOKENS` | empty | bearer tokens |
| `RACEGUARD_MCP_ALLOW_ANONYMOUS` | `false` | HTTP without tokens, loopback only |
| `RACEGUARD_MCP_ALLOWED_HOSTS` | bind host | DNS rebinding allowlist |
| `RACEGUARD_MCP_SESSION_CAP`, `_TARGET_CAP`, `_MAX_VERIFIES` | 20, 6, 1 | budgets |
| `RACEGUARD_MCP_VERIFY` | `true` | disable verification entirely |
| `RACEGUARD_MCP_METRICS` | `true` | serve `/metrics` |
| `RACEGUARD_LOG_FORMAT` | text (json over HTTP) | stderr log shape |
| `RACEGUARD_MCP_SHUTDOWN_GRACE` | 30 | drain seconds on SIGTERM |

`raceguard.mcp_server` remains as a shim for hosts configured against the old
module path.
