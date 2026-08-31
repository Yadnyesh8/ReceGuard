# Security and the sandbox

RaceGuard executes other people's code and other people's SQL. That is a design
constraint from the first commit, not a hardening pass at the end.

## Container mode, the default

The application under test runs with:

* no capabilities (`--cap-drop ALL`, `--security-opt no-new-privileges`),
* a read-only root filesystem with a small writable tmpfs,
* memory and CPU quotas,
* the source directory mounted read-only,
* a wall-clock kill,
* a label `raceguard.run=<id>` so cleanup is a label filter rather than a name
  guess.

## inprocess mode is strictly less safe

`inprocess` imports the application under test into the RaceGuard process. It
exists because it drops boot cost from roughly ten seconds to under one, which
is what makes the editor loop usable, and it cannot reproduce races that depend
on more than one process.

The warning is printed **once per run**, not buried here. Anyone running
RaceGuard against a repository they do not control should use container mode,
and the MCP server defaults to container mode when it detects it is running
outside the user's own project directory.

## The governor is not optional

RaceGuard fires bursts of concurrent requests in a loop. Pointed at a shared
environment it will take that environment down, and the tool will be banned
internally the same afternoon.

* **Ownership check.** Before any run, the resolved database URL must be an
  ephemeral instance RaceGuard created. Otherwise it refuses.
* **`--allow-external-target` requires typing the host name** to confirm, and
  even then the concurrency ceiling drops to 4 and amplification is disabled.
* **Backpressure.** On 429 or 503 the run stops, honours `Retry-After`, then
  `RateLimit-Reset`, then exponential backoff with jitter. Continuing to send
  while limited is what gets integrations banned.
* **Advertised limits are respected.** If the target advertises a limit below
  the configured concurrency, the target is refused with the reason.

## Secrets

Environment interpolation in the config resolves at load time and the resolved
values never enter run artifacts. Header values matching a credential pattern
are redacted before serialisation, and response bodies are truncated. Those run
folders get pasted into issues.

An undefined environment variable is an **error**, not an empty string.
Substituting `""` silently produces an unauthenticated request that 401s, after
which the run would report "not reproduced", which would be a lie.

## Supply chain

Pin the PostgreSQL image by digest, pin every dependency with a lock file, and
enable Dependabot. `raceguard doctor` warns when the image is pinned only by
tag, because a moved tag can silently change verification results.

A tool that people run with Docker access is a tool worth attacking.

## Reporting a vulnerability

Open a private security advisory on the repository rather than a public issue.
