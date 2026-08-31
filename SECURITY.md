# Security policy

RaceGuard executes other people's code and other people's SQL, and it needs
Docker access to do its job. That makes it a tool worth attacking, and we treat
reports accordingly.

## Reporting a vulnerability

**Do not open a public issue.** Use GitHub's private vulnerability reporting
(Security → Report a vulnerability) on this repository.

Please include the version, the command you ran, and a minimal reproduction. If
you cannot use GitHub advisories, the maintainers' contact address is in
`pyproject.toml`.

We will acknowledge within 3 working days and aim to ship a fix or a documented
mitigation within 30 days for anything rated high or critical.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | yes |

Pre-1.0, only the latest minor line receives fixes.

## Threat model

What RaceGuard assumes, so you know where the boundary is.

**Trusted:** the machine it runs on, the Docker daemon it talks to, and the
`raceguard.yaml` in the repository being scanned.

**Untrusted:** the application source under analysis, its SQL, its migrations,
and any response body it returns.

### Where the untrusted code actually runs

* **`mode: container` (the default).** The application runs with all
  capabilities dropped, `no-new-privileges`, a read-only root filesystem, a
  small `noexec,nosuid` tmpfs, memory and CPU quotas, and no host mount of your
  source. Only a generated instrumentation shim is mounted, read-only, from a
  temporary directory.
* **`mode: inprocess`.** The application is **imported into the RaceGuard
  process**. This is strictly less safe and exists only because it makes the
  editor loop usable. RaceGuard prints that warning once per run. Do not use it
  on a repository you do not control.

Migrations (`app.migrate`) and your test suite (`app.test_command`) are shell
commands from the config, executed on the host in both modes. Treat a
`raceguard.yaml` from an untrusted source the way you would treat a Makefile
from one.

### Denial of service, by construction

RaceGuard is a load generator. The governor is mandatory, not a setting: a
global concurrency ceiling, minimum spacing between trials, a hard per-run
request budget, an outbound token bucket, and backpressure that stops on 429 or
503 and honours `Retry-After`.

Before any run it verifies the resolved database URL belongs to an ephemeral
instance it created. `--allow-external-target` requires typing the host name to
confirm, and even then the concurrency ceiling drops and amplification is
disabled.

### Secrets

Environment interpolation resolves at load time and resolved values never enter
run artifacts under `.raceguard/runs/`. Header values matching a credential
pattern are redacted before serialisation and response bodies are truncated,
because those folders get pasted into issues.

An undefined environment variable is an error rather than an empty string:
substituting `""` silently produces an unauthenticated request that 401s, after
which the run would report "not reproduced", which would be a lie.

## Out of scope

* Findings from running RaceGuard against deliberately malicious source in
  `inprocess` mode. That is documented as unsafe; use container mode.
* Resource exhaustion caused by raising the governor limits yourself.
* The deliberately vulnerable reference application in `examples/shop`. Its
  race conditions are the point.
