"""The CI engine (docs/ci-and-mcp-spec.md section 3). No Docker, no network."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from raceguard.ci import CIOptions, github_annotations, run, summary
from raceguard.ci.baseline import Baseline
from raceguard.ci.comment import MARKER, post_sticky
from raceguard.ci.providers import ProviderContext, changed_files, detect, github_annotation
from raceguard.contracts import CodeSpan, Confidence, Finding, ScanResult
from raceguard.errors import EXIT_CONFIG, EXIT_FINDINGS, EXIT_INFRA, EXIT_OK, ConfigError
from raceguard.report import codequality

VULNERABLE = """\
from fastapi import APIRouter, HTTPException
router = APIRouter()

@router.post("/orders")
async def create_order(item_id: int, qty: int, session):
    item = await session.get(Item, item_id)
    if item.stock < qty:
        raise HTTPException(409, "no")
    item.stock -= qty
    await session.commit()
    return {}
"""

SAFE = """\
from fastapi import APIRouter
router = APIRouter()

@router.get("/health")
async def health():
    return {"ok": True}
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main has a safe file; a feature branch adds the vulnerable one."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "safe.py").write_text(SAFE)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "checkout", "-qb", "feature")
    (tmp_path / "orders.py").write_text(VULNERABLE)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "add orders")
    return tmp_path


# -- providers ------------------------------------------------------------


def test_github_pull_request_is_detected_with_its_base():
    ctx = detect(
        {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_BASE_REF": "main",
            "GITHUB_REF": "refs/pull/42/merge",
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_TOKEN": "t",
        }
    )
    assert ctx.name == "github"
    assert ctx.is_pull_request and ctx.base_ref == "origin/main"
    assert ctx.pr_number == 42 and ctx.repo == "o/r" and ctx.token == "t"
    assert ctx.changed_only_default is True


def test_github_push_uses_the_previous_sha_and_gates_everything():
    ctx = detect(
        {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_BEFORE": "abc123"}
    )
    assert ctx.base_ref == "abc123" and not ctx.is_pull_request
    assert ctx.changed_only_default is False
    first_push = detect(
        {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_BEFORE": "0" * 40}
    )
    assert first_push.base_ref is None


def test_gitlab_merge_request_is_detected():
    ctx = detect(
        {
            "GITLAB_CI": "true",
            "CI_MERGE_REQUEST_IID": "7",
            "CI_MERGE_REQUEST_DIFF_BASE_SHA": "deadbeef",
            "CI_PROJECT_ID": "123",
            "CI_JOB_TOKEN": "job",
        }
    )
    assert ctx.name == "gitlab" and ctx.is_pull_request
    assert ctx.base_ref == "deadbeef" and ctx.pr_number == 7 and ctx.token == "job"


def test_generic_provider_has_no_base_and_gates_everything():
    ctx = detect({"CI": "true", "BUILDKITE": "true"})
    assert ctx.name == "generic" and ctx.base_ref is None and not ctx.changed_only_default


def test_forced_provider_wins_over_the_environment():
    assert detect({"GITHUB_ACTIONS": "true"}, forced="generic").name == "generic"


# -- changed files ----------------------------------------------------------


def test_changed_files_against_a_real_diff(repo: Path):
    files, notice = changed_files("main", cwd=repo)
    assert files == {"orders.py"} and notice is None


def test_unknown_base_degrades_with_a_notice(repo: Path):
    files, notice = changed_files("no-such-ref", cwd=repo)
    assert files == set() and notice and "gating on every file" in notice


# -- gating -----------------------------------------------------------------


def _options(repo: Path, **overrides) -> CIOptions:
    base = dict(
        path=str(repo), provider="generic", sarif=None, annotations=False, hygiene=False, env={}
    )
    base.update(overrides)
    return CIOptions(**base)


def test_a_high_finding_gates_by_default(repo: Path):
    result = run(_options(repo))
    assert result.exit_code == EXIT_FINDINGS
    assert [f.read_site.file for f in result.gated] == ["orders.py"]


def test_fail_on_never_reports_but_does_not_gate(repo: Path):
    result = run(_options(repo, fail_on="never"))
    assert result.exit_code == EXIT_OK and result.scan.findings and not result.gated


def test_changed_only_gates_only_the_diff(repo: Path):
    # Move the vulnerable file into the base so it is NOT in the diff.
    _git(repo, "checkout", "-q", "main")
    (repo / "orders.py").write_text(VULNERABLE)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "orders on main")
    _git(repo, "checkout", "-qb", "feature2")
    (repo / "other.py").write_text(SAFE)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unrelated")

    result = run(_options(repo, base="main", changed_only=True))
    assert result.changed_files == {"other.py"}
    assert result.exit_code == EXIT_OK
    assert [f.read_site.file for f in result.out_of_diff] == ["orders.py"]


def test_changed_only_with_an_empty_diff_gates_everything_and_says_so(repo: Path):
    result = run(_options(repo, base="no-such-ref", changed_only=True))
    assert result.changed_only is False
    assert result.exit_code == EXIT_FINDINGS
    assert any("gating on every file" in n for n in result.notices)


# -- baseline ---------------------------------------------------------------


def test_baseline_round_trip_suppresses_gating_and_reports_stale(repo: Path):
    first = run(_options(repo))
    baseline = Baseline.from_findings(first.scan.findings)
    baseline.fingerprints.add("deadbeefdeadbeef")  # a finding that no longer exists
    path = baseline.write(repo / ".raceguard" / "baseline.json")

    result = run(_options(repo, baseline=str(path.relative_to(repo))))
    assert result.exit_code == EXIT_OK
    assert len(result.baselined) == 1 and not result.gated
    assert result.stale_baseline == {"deadbeefdeadbeef"}
    assert "stale" in result.summary_markdown


def test_baseline_rejects_a_malformed_file(tmp_path: Path):
    bad = tmp_path / "b.json"
    bad.write_text('{"version": 99}')
    with pytest.raises(ConfigError):
        Baseline.load(bad)
    with pytest.raises(ConfigError):
        Baseline.load(tmp_path / "missing.json")


def test_fingerprint_survives_a_line_shift_and_differs_by_route():
    def finding(line: int, route: str) -> Finding:
        span = CodeSpan(file="a.py", start_line=line, end_line=line)
        return Finding(
            id=Finding.make_id("a.py", "m:handler", "oversell", line),
            handler="m:handler",
            route=route,
            pattern="oversell",
            read_site=span,
            write_site=span,
            confidence=Confidence.high,
            explanation="",
        )

    assert finding(6, "POST /a").fingerprint == finding(60, "POST /a").fingerprint
    assert finding(6, "POST /a").fingerprint != finding(6, "POST /b").fingerprint
    assert finding(6, "POST /a").id != finding(60, "POST /a").id


# -- outputs ----------------------------------------------------------------


def test_every_report_is_written_even_when_gated(repo: Path):
    result = run(
        _options(
            repo,
            sarif="out/r.sarif",
            json_path="out/r.json",
            codequality="out/cq.json",
            summary_path="out/summary.md",
        )
    )
    assert result.exit_code == EXIT_FINDINGS
    assert set(result.written) == {"sarif", "json", "codequality", "summary"}
    sarif = json.loads((repo / "out/r.sarif").read_text())
    fingerprint = sarif["runs"][0]["results"][0]["partialFingerprints"]["primaryLocationLineHash"]
    assert fingerprint == result.gated[0].fingerprint
    document = json.loads((repo / "out/r.json").read_text())
    assert document["ci"]["gated"] == [result.gated[0].id]
    assert "## RaceGuard" in (repo / "out/summary.md").read_text()


def test_code_quality_report_shape_and_severity():
    span = CodeSpan(file="a.py", start_line=3, end_line=3)
    finding = Finding(
        id="x",
        handler="m:h",
        route="POST /x",
        pattern="oversell",
        read_site=span,
        write_site=span,
        confidence=Confidence.high,
        explanation="why",
    )
    issues = codequality.build(ScanResult(findings=[finding]))
    assert issues == [
        {
            "type": "issue",
            "check_name": "raceguard/oversell",
            "description": "why",
            "categories": ["Bug Risk"],
            "severity": "major",
            "fingerprint": finding.fingerprint,
            "location": {"path": "a.py", "lines": {"begin": 3}},
        }
    ]


def test_annotations_mark_gated_as_errors_and_cap_at_fifty(repo: Path):
    result = run(_options(repo))
    lines = github_annotations(result)
    assert lines and lines[0].startswith("::error file=orders.py,line=")
    assert "title=raceguard%3A oversell" in lines[0]
    result.gated = result.gated * 60
    assert len(github_annotations(result)) == 50


def test_annotation_escapes_newlines_and_percent():
    line = github_annotation("warning", "a.py", 1, "t", "100%\nnext")
    assert line == "::warning file=a.py,line=1,title=t::100%25%0Anext"


def test_summary_lists_status_per_finding(repo: Path):
    result = run(_options(repo))
    text = summary(result)
    assert "| gated | high | `oversell` | `POST /orders` | `orders.py:" in text
    assert "1 finding(s) gate this build" in text
    assert "not a proof of correctness" in text


def test_github_outputs_and_step_summary_are_written(repo: Path, tmp_path: Path):
    outputs = tmp_path / "out.txt"
    step = tmp_path / "step.md"
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_STEP_SUMMARY": str(step),
    }
    result = run(_options(repo, provider="auto", env=env))
    assert result.provider.name == "github"
    written = outputs.read_text()
    assert "findings=1" in written and "gated=1" in written and "exit-code=1" in written
    assert "## RaceGuard" in step.read_text()


# -- sticky comment -----------------------------------------------------------


def _github_context(token: str | None = "tok") -> ProviderContext:
    return ProviderContext(
        name="github",
        base_ref="origin/main",
        is_pull_request=True,
        repo="o/r",
        pr_number=5,
        api_url="https://api.example",
        token=token,
        env={},
    )


def test_sticky_comment_is_created_when_absent():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 1, "body": "unrelated"}])
        assert MARKER in json.loads(request.content)["body"]
        return httpx.Response(201, json={"id": 2})

    result = post_sticky(_github_context(), "hello", transport=httpx.MockTransport(handler))
    assert result.posted and result.action == "created"
    assert calls == [
        ("GET", "/repos/o/r/issues/5/comments"),
        ("POST", "/repos/o/r/issues/5/comments"),
    ]


def test_sticky_comment_is_updated_in_place_when_present():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 9, "body": f"{MARKER}\nold"}])
        return httpx.Response(200, json={"id": 9})

    result = post_sticky(_github_context(), "new", transport=httpx.MockTransport(handler))
    assert result.action == "updated"
    assert calls[-1] == ("PATCH", "/repos/o/r/issues/comments/9")


def test_no_token_means_no_request_and_no_failure():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be made without a token")

    result = post_sticky(_github_context(token=None), "x", transport=httpx.MockTransport(handler))
    assert not result.posted and "no token" in result.reason


def test_gitlab_note_uses_the_job_token_header():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"id": 1})

    ctx = ProviderContext(
        name="gitlab",
        base_ref="x",
        is_pull_request=True,
        repo="123",
        pr_number=7,
        api_url="https://gl.example/api/v4",
        token="job",
        env={"CI_JOB_TOKEN": "job"},
    )
    result = post_sticky(ctx, "x", transport=httpx.MockTransport(handler))
    assert result.action == "created"
    assert all(h.get("job-token") == "job" for h in seen)


# -- verification branches ------------------------------------------------


def test_verify_without_a_config_skips_or_fails_as_configured(repo: Path):
    result = run(_options(repo, verify=True, fail_on="never"))
    assert result.exit_code == EXIT_OK
    assert any("no raceguard.yaml" in n for n in result.notices)
    required = run(_options(repo, verify=True, verify_required=True, fail_on="never"))
    assert required.exit_code == EXIT_CONFIG


def test_verify_without_docker_skips_or_fails_as_configured(repo: Path, monkeypatch):
    (repo / "raceguard.yaml").write_text("version: 1\napp:\n  module: 'm:app'\ntargets: []\n")
    monkeypatch.setattr("raceguard.ci._docker_reachable", lambda: False)
    result = run(_options(repo, verify=True, fail_on="never"))
    assert result.exit_code == EXIT_OK and any("Docker" in n for n in result.notices)
    required = run(_options(repo, verify=True, verify_required=True, fail_on="never"))
    assert required.exit_code == EXIT_INFRA


def test_the_action_entrypoint_maps_inputs_onto_the_engine(repo: Path, monkeypatch, capsys):
    from raceguard.action import entrypoint

    monkeypatch.chdir(repo)
    for key, value in {
        "INPUT_PATH": ".",
        "INPUT_FAIL_ON": "never",
        "INPUT_SARIF_FILE": "",
        "INPUT_HYGIENE": "false",
        "INPUT_ANNOTATIONS": "false",
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "push",
    }.items():
        monkeypatch.setenv(key, value)
    assert entrypoint.main() == EXIT_OK
    assert "oversell" in capsys.readouterr().out


def test_verdicts_are_linked_back_to_their_finding(repo: Path):
    """A reproduced violation escalates in SARIF and Code Quality, and every
    report keys that on finding_id. An empty finding_id silently downgrades a
    PROVEN race to an ordinary static suspicion, which is the one thing a
    verification run exists to prevent."""
    from raceguard.ci import findings_by_target
    from raceguard.config import Config, Target

    result = run(_options(repo, fail_on="never"))
    finding = next(f for f in result.scan.findings if f.route == "POST /orders")
    config = Config(
        app={"module": "m:app"},
        targets=[Target(id="purchase", route="POST /orders")],
    )
    assert findings_by_target(config, result.scan.findings) == {"purchase": finding.id}


def test_a_verified_violation_escalates_in_code_quality():
    from raceguard.contracts import Invariant, Verdict

    span = CodeSpan(file="a.py", start_line=3, end_line=3)
    finding = Finding(
        id="fid",
        handler="m:h",
        route="POST /x",
        pattern="oversell",
        read_site=span,
        write_site=span,
        confidence=Confidence.high,
        explanation="why",
    )
    invariant = Invariant(
        kind="non_negative",
        table="items",
        column="stock",
        predicate="p",
        check_sql="SELECT 1",
        source="config",
        derivation="d",
    )
    verdict = Verdict(
        finding_id="fid",
        target_id="purchase",
        invariant=invariant,
        violated=True,
        violating_trials=7,
        total_trials=7,
        usable_trials=7,
    )
    issue = codequality.build(ScanResult(findings=[finding]), verdicts=[verdict])[0]
    assert issue["severity"] == "critical"
    assert issue["description"].startswith("VERIFIED:")
    # and an unlinked verdict must NOT escalate some unrelated finding
    orphan = verdict.model_copy(update={"finding_id": ""})
    assert (
        codequality.build(ScanResult(findings=[finding]), verdicts=[orphan])[0]["severity"]
        == "major"
    )


def test_a_changed_handler_is_verified_even_with_no_finding_left():
    """The honest-fix rule. Edit a handler so the scanner no longer recognises
    the pattern and the finding disappears; if selection depended only on
    findings, CI would verify nothing and go green on a handler that may still
    race. A configured target whose handler file changed gets proven."""
    from raceguard.ci import targets_in_diff
    from raceguard.config import Config, Target

    config = Config(
        app={"module": "m:app"},
        targets=[
            Target(
                id="purchase", route="POST /orders", handler="shop/routes/orders.py::create_order"
            ),
            Target(id="signup", route="POST /signup", handler="shop/routes/signup.py::signup"),
        ],
    )
    # No findings at all, but the handler file is in the diff.
    assert targets_in_diff(config, [], {"shop/routes/orders.py"}) == ["purchase"]
    # An untouched target stays untouched.
    assert targets_in_diff(config, [], {"README.md"}) == []


def test_a_finding_on_a_changed_route_still_selects_its_target(repo: Path):
    from raceguard.ci import targets_in_diff
    from raceguard.config import Config, Target

    result = run(_options(repo, fail_on="never"))
    # handler deliberately unset: selection must still work off the finding.
    config = Config(app={"module": "m:app"}, targets=[Target(id="purchase", route="POST /orders")])
    assert targets_in_diff(config, result.scan.findings, {"orders.py"}) == ["purchase"]
