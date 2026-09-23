# RaceGuard in CI

One command does the work everywhere:

```
raceguard ci [PATH] [--provider auto|github|gitlab|generic] [--base REF]
             [--changed-only|--all-files] [--fail-on high|medium|low|never]
             [--baseline .raceguard/baseline.json]
             [--verify] [--verify-required] [--budget-seconds N]
             [--sarif F] [--json F] [--codequality F] [--summary F]
             [--annotations|--no-annotations] [--comment]
```

It scans, gates, optionally verifies the targets whose handler changed, and
writes every report before deciding the exit code, so a red build still ships
its SARIF and its summary.

Exit codes: `0` clean, `1` gated findings or a verified violation, `2`
configuration error, `3` infrastructure error (Docker missing with
`--verify-required`, a container that would not start).

## What gates a build

A finding fails the build only when all of these hold: its confidence is at or
above `--fail-on`, it is not suppressed, its fingerprint is not in the
baseline, and (on pull requests, by default) its file is in the diff. A
verification verdict that reproduced a violation always gates.

Findings outside the diff and findings in the baseline are still reported, as
notices and in the SARIF, so nothing is hidden; they just do not block.

## Adopting on an existing codebase

```
raceguard baseline write            # records every current finding's fingerprint
git add .raceguard/baseline.json
raceguard ci --baseline .raceguard/baseline.json
```

The baseline matches on a fingerprint that ignores line numbers, so editing
code above a handler does not resurrect it. Every run reports stale entries
(findings that no longer exist) so the file shrinks as debt is paid.
`raceguard baseline check` is the same comparison as a standalone gate.

## GitHub Actions

```yaml
name: raceguard
on: [push, pull_request]
permissions:
  contents: read
  security-events: write   # upload-sarif
  pull-requests: write     # comment: true
jobs:
  raceguard:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0     # changed-only gating needs the merge base
      - uses: raceguard/raceguard@v1
        id: raceguard
        with:
          fail-on: high
          baseline: .raceguard/baseline.json
          comment: true
          verify: true               # only targets whose handler changed
          budget-seconds: 300
      - uses: github/codeql-action/upload-sarif@v3
        if: always()
        with:
          sarif_file: ${{ steps.raceguard.outputs.sarif }}
          category: raceguard
```

Inline annotations need no upload permission; they are workflow commands the
runner renders on the diff. The sticky comment is created once per pull
request and edited on later pushes. The step summary carries the same
Markdown. `version:` pins a PyPI release; empty installs the action's own
checkout, which works before any release exists.

## GitLab CI

```yaml
include:
  - remote: https://raw.githubusercontent.com/raceguard/raceguard/main/ci/gitlab/raceguard.gitlab-ci.yml
variables:
  RACEGUARD_FAIL_ON: high
  RACEGUARD_BASELINE: .raceguard/baseline.json
  RACEGUARD_VERIFY: "true"     # adds the raceguard:verify job with docker:dind
```

The `raceguard` job publishes `gl-code-quality-report.json` (inline in the
merge request widget) and `raceguard.sarif` (the SAST tab). `--comment` uses
`GITLAB_TOKEN` or falls back to `CI_JOB_TOKEN`.

## Everyone else

The generic provider has no base ref of its own; pass one.

```
# Buildkite
raceguard ci --base "origin/${BUILDKITE_PULL_REQUEST_BASE_BRANCH:-main}" --sarif raceguard.sarif
# CircleCI
raceguard ci --base "origin/${CIRCLE_BRANCH_BASE:-main}"
# Jenkins (multibranch)
raceguard ci --base "origin/${CHANGE_TARGET:-main}"
# Azure Pipelines
raceguard ci --base "origin/$(System.PullRequest.TargetBranch)"
```

Then upload `raceguard.sarif`, `raceguard.json` or the summary as an artifact
with the host's own step. Without `--base` the run gates on every file, which
is right for a nightly job and wrong for a pull request.

## Verification in CI

`--verify` runs only the targets whose handler file appears in the diff, so a
pull request that touches one route verifies one route. `--budget-seconds`
caps the whole phase; targets that did not fit are listed as skipped, never
silently dropped. A runner without Docker skips verification with a notice;
`--verify-required` makes that exit 3 instead.

`verify` on every push for every target is the shape that gets a check
removed from the pipeline. The engine will not do it unless you pass
`--verify --all-files` explicitly.
