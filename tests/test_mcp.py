"""The MCP framework (docs/ci-and-mcp-spec.md section 4). No Docker, no network."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from raceguard.errors import ConfigError, UsageError
from raceguard.mcp.auth import StaticTokenVerifier, client_id_for, parse_tokens
from raceguard.mcp.budget import Budget, current_client
from raceguard.mcp.install import install, snippet
from raceguard.mcp.sandbox import Sandbox, SandboxViolation
from raceguard.mcp.server import build, build_http_app
from raceguard.mcp.settings import MCPSettings

SHOP = Path(__file__).resolve().parents[1] / "examples" / "shop"


# -- settings -----------------------------------------------------------------


def test_defaults_are_the_safe_ones():
    s = MCPSettings.from_env({})
    assert s.transport == "stdio" and s.host == "127.0.0.1"
    assert s.tokens == [] and not s.allow_anonymous and not s.allow_any_path


def test_http_without_tokens_is_refused_unless_anonymous_on_loopback():
    with pytest.raises(ConfigError):
        MCPSettings.from_env({"RACEGUARD_MCP_TRANSPORT": "http"}).validate()
    MCPSettings.from_env(
        {"RACEGUARD_MCP_TRANSPORT": "http", "RACEGUARD_MCP_ALLOW_ANONYMOUS": "true"}
    ).validate()
    with pytest.raises(ConfigError):
        MCPSettings.from_env(
            {
                "RACEGUARD_MCP_TRANSPORT": "http",
                "RACEGUARD_MCP_ALLOW_ANONYMOUS": "true",
                "RACEGUARD_MCP_HOST": "0.0.0.0",
            }
        ).validate()


def test_allow_any_path_is_ignored_over_http():
    s = MCPSettings.from_env(
        {
            "RACEGUARD_MCP_TRANSPORT": "http",
            "RACEGUARD_MCP_TOKENS": "t",
            "RACEGUARD_MCP_ALLOW_ANY_PATH": "1",
        }
    )
    s.validate()
    assert s.allow_any_path is False


def test_unknown_transport_is_a_config_error():
    with pytest.raises(ConfigError):
        MCPSettings.from_env({"RACEGUARD_MCP_TRANSPORT": "sse"})


# -- sandbox ------------------------------------------------------------------


def test_sandbox_allows_inside_and_refuses_outside(tmp_path: Path):
    (tmp_path / "app").mkdir()
    box = Sandbox(tmp_path)
    assert box.resolve("app") == (tmp_path / "app").resolve()
    assert box.resolve(".") == tmp_path.resolve()
    with pytest.raises(SandboxViolation):
        box.resolve("..")
    with pytest.raises(SandboxViolation):
        box.resolve("/etc")


def test_sandbox_follows_symlinks_out_and_refuses(tmp_path: Path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SandboxViolation):
        Sandbox(root).resolve("link")


def test_sandbox_can_be_disabled_for_stdio(tmp_path: Path):
    box = Sandbox(tmp_path, enforce=False)
    assert box.resolve("/etc") == Path("/etc").resolve()


def test_anchor_prefers_the_config_directory():
    box = Sandbox(SHOP)
    assert box.anchor(SHOP / "shop" / "routes") == SHOP.resolve()


# -- budgets ------------------------------------------------------------------


def test_budgets_are_keyed_per_client():
    budget = Budget(session_cap=1, target_cap_per_hour=10, max_concurrent=1)
    assert budget.check("a", "t") is None
    budget.record("a", "t")
    assert budget.check("a", "t") is not None  # a is capped
    assert budget.check("b", "t") is None  # b is not
    assert budget.refusals == 1


def test_per_target_cap_carries_a_retry_hint():
    budget = Budget(session_cap=100, target_cap_per_hour=1, max_concurrent=1)
    budget.record("a", "t")
    refusal = budget.check("a", "t")
    assert refusal is not None and refusal.retry_after_s is not None


def test_verify_slots_return_busy_rather_than_blocking():
    budget = Budget(session_cap=100, target_cap_per_hour=100, max_concurrent=1)
    assert budget.acquire() is True
    assert budget.acquire() is False
    assert budget.in_flight == 1
    budget.release()
    assert budget.acquire() is True and budget.in_flight == 1


def test_closed_budget_refuses_and_drain_waits_for_in_flight():
    budget = Budget(session_cap=100, target_cap_per_hour=100, max_concurrent=1)
    budget.acquire()
    budget.close()
    assert "shutting down" in (budget.check("a", "t") or Budget.__name__).__str__()
    assert budget.drain(0.2) is False
    budget.release()
    assert budget.drain(0.2) is True


# -- auth ---------------------------------------------------------------------


def test_tokens_parse_with_plus_separated_scopes():
    assert parse_tokens(["abc:read+verify", "def", " ", ":x"]) == {
        "abc": ["read", "verify"],
        "def": [],
    }


def test_verifier_accepts_known_and_rejects_unknown():
    verifier = StaticTokenVerifier(["abc:verify"])
    access = verifier.verify("abc")
    assert access is not None and access.scopes == ["verify"]
    assert access.subject == client_id_for("abc") and not access.subject.startswith("abc")
    assert verifier.verify("abd") is None
    assert asyncio.run(verifier.verify_token("abc")) is not None


# -- the server, in memory ------------------------------------------------


def _settings(**overrides) -> MCPSettings:
    base = dict(
        transport="http", tokens=["secret"], project_root=SHOP.resolve(), verify_enabled=False
    )
    base.update(overrides)
    return MCPSettings(**base)


class _App:
    """Drive the ASGI lifespan so the SDK's session manager is running."""

    def __init__(self, settings: MCPSettings) -> None:
        self.app, self.runtime = build_http_app(settings)

    async def __aenter__(self):
        self._started = asyncio.Event()
        self._stop = asyncio.Event()
        self._queue = [{"type": "lifespan.startup"}]

        async def receive():
            if self._queue:
                return self._queue.pop(0)
            await self._stop.wait()
            return {"type": "lifespan.shutdown"}

        async def send(message):
            if message["type"] == "lifespan.startup.complete":
                self._started.set()

        self._task = asyncio.create_task(
            self.app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
        )
        await self._started.wait()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:8765"
        )
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()
        self._stop.set()
        await self._task

    async def rpc(self, method: str, params: dict | None = None, *, token: str | None = "secret"):
        headers = {"Accept": "application/json, text/event-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        return await self.client.post("/mcp", json=body, headers=headers)


def test_health_endpoints_need_no_token():
    async def go():
        async with _App(_settings()) as app:
            assert (await app.client.get("/healthz")).json() == {"status": "ok"}
            ready = await app.client.get("/readyz")
            assert ready.status_code == 200 and ready.json()["status"] == "ready"
            version = (await app.client.get("/version")).json()
            assert version["raceguard"] and version["transport"] == "streamable-http"
            metrics = await app.client.get("/metrics")
            assert metrics.status_code == 200
            assert metrics.headers["content-type"].startswith("text/plain")
            assert "raceguard_mcp_uptime_seconds" in metrics.text

    asyncio.run(go())


def test_metrics_can_be_disabled():
    async def go():
        async with _App(_settings(metrics_enabled=False)) as app:
            assert (await app.client.get("/metrics")).status_code == 404

    asyncio.run(go())


def test_mcp_requires_a_bearer_token():
    async def go():
        async with _App(_settings()) as app:
            missing = await app.rpc("tools/list", token=None)
            assert missing.status_code == 401
            assert missing.headers["www-authenticate"].startswith("Bearer")
            wrong = await app.rpc("tools/list", token="nope")
            assert wrong.status_code == 401
            right = await app.rpc("tools/list")
            assert right.status_code == 200

    asyncio.run(go())


def test_anonymous_loopback_is_allowed_when_configured():
    async def go():
        async with _App(_settings(tokens=[], allow_anonymous=True)) as app:
            assert (await app.rpc("tools/list", token=None)).status_code == 200

    asyncio.run(go())


def test_every_tool_is_listed_with_cost_and_annotations():
    async def go():
        async with _App(_settings()) as app:
            tools = (await app.rpc("tools/list")).json()["result"]["tools"]
            by_name = {t["name"]: t for t in tools}
            assert set(by_name) == {
                "analyze_handler",
                "check_hygiene",
                "get_finding",
                "list_targets",
                "suggest_fix",
                "get_run",
                "doctor",
                "verify_handler",
            }
            for name, tool in by_name.items():
                first = tool["description"].strip().splitlines()[0]
                assert first.startswith(("CHEAP", "EXPENSIVE")), name
            assert by_name["suggest_fix"]["annotations"]["readOnlyHint"] is True
            assert by_name["verify_handler"]["annotations"]["readOnlyHint"] is False
            assert by_name["verify_handler"]["annotations"]["destructiveHint"] is False

    asyncio.run(go())


def test_analyze_handler_returns_structured_findings_inside_the_sandbox():
    async def go():
        async with _App(_settings()) as app:
            r = await app.rpc(
                "tools/call", {"name": "analyze_handler", "arguments": {"path": "shop"}}
            )
            result = r.json()["result"]
            assert result["isError"] is False
            payload = result["structuredContent"]["result"]
            assert payload["files_scanned"] > 0 and payload["findings"]
            assert "suspicion" in payload["note"]

    asyncio.run(go())


def test_sandbox_escape_is_a_structured_refusal_not_an_exception():
    async def go():
        async with _App(_settings()) as app:
            r = await app.rpc(
                "tools/call", {"name": "analyze_handler", "arguments": {"path": "/etc"}}
            )
            payload = r.json()["result"]["structuredContent"]["result"]
            assert "outside the project root" in payload["error"]
            assert "--project-root" in payload["hint"]

    asyncio.run(go())


def test_verify_disabled_refuses_cleanly():
    async def go():
        async with _App(_settings()) as app:
            r = await app.rpc(
                "tools/call", {"name": "verify_handler", "arguments": {"target_id": "purchase"}}
            )
            payload = r.json()["result"]["structuredContent"]
            assert payload["summary"] == "refused: disabled"

    asyncio.run(go())


def test_verify_unknown_target_is_refused_without_booting_anything():
    async def go():
        async with _App(_settings(verify_enabled=True)) as app:
            r = await app.rpc(
                "tools/call", {"name": "verify_handler", "arguments": {"target_id": "nope"}}
            )
            payload = r.json()["result"]["structuredContent"]
            assert payload["summary"] == "refused: unknown target"
            assert app.runtime.budget.in_flight == 0

    asyncio.run(go())


def test_resources_and_prompts_are_registered():
    async def go():
        async with _App(_settings()) as app:
            resources = (await app.rpc("resources/list")).json()["result"]["resources"]
            assert [r["uri"] for r in resources] == ["raceguard://config"]
            templates = (await app.rpc("resources/templates/list")).json()["result"][
                "resourceTemplates"
            ]
            uris = {t["uriTemplate"] for t in templates}
            assert uris == {
                "raceguard://runs/{run_id}",
                "raceguard://findings/{finding_id}",
                "raceguard://docs/{name}",
            }
            prompts = (await app.rpc("prompts/list")).json()["result"]["prompts"]
            assert {p["name"] for p in prompts} == {"triage_finding", "explain_verdict"}
            doc = (
                await app.rpc("resources/read", {"uri": "raceguard://docs/reproduction-rate"})
            ).json()
            assert "reproduction rate" in doc["result"]["contents"][0]["text"].lower()
            config = (await app.rpc("resources/read", {"uri": "raceguard://config"})).json()
            assert "targets" in json.loads(config["result"]["contents"][0]["text"])
            prompt = (
                await app.rpc(
                    "prompts/get",
                    {"name": "explain_verdict", "arguments": {"target_id": "purchase"}},
                )
            ).json()
            text = prompt["result"]["messages"][0]["content"]["text"]
            assert "Never write 'safe'" in text

    asyncio.run(go())


def test_docs_resource_does_not_traverse():
    async def go():
        async with _App(_settings()) as app:
            r = await app.rpc("resources/read", {"uri": "raceguard://docs/../../pyproject"})
            text = r.json().get("result", {}).get("contents", [{}])[0].get("text", "")
            assert "no document named" in text or r.json().get("error")

    asyncio.run(go())


def test_client_identity_reaches_the_budget():
    """The middleware sets the client from the token; a second token is a
    second budget. Proven through the real protocol, not by calling the tool."""

    async def go():
        async with _App(_settings(tokens=["a", "b"], session_cap=1, verify_enabled=True)) as app:
            # Unknown target still records nothing, so force the budget through check():
            assert app.runtime.budget.check(client_id_for("a"), "x") is None
            app.runtime.budget.record(client_id_for("a"), "x")
            r = await app.rpc(
                "tools/call",
                {"name": "verify_handler", "arguments": {"target_id": "purchase"}},
                token="a",
            )
            assert r.json()["result"]["structuredContent"]["summary"] == "refused: budget"
            r = await app.rpc(
                "tools/call",
                {"name": "verify_handler", "arguments": {"target_id": "nope"}},
                token="b",
            )
            assert r.json()["result"]["structuredContent"]["summary"] == "refused: unknown target"

    asyncio.run(go())


def test_stdio_build_registers_no_http_routes():
    mcp, runtime = build(MCPSettings(transport="stdio", project_root=SHOP.resolve()))
    assert current_client.get() == "stdio"
    assert runtime.sandbox.enforce is True


def test_shutdown_drains_in_flight_verifications(monkeypatch):
    async def go():
        settings = _settings(shutdown_grace_s=1)
        app = _App(settings)
        async with app:
            app.runtime.budget.acquire()
            release_after = asyncio.get_running_loop().call_later(0.3, app.runtime.budget.release)
            assert release_after is not None
        # __aexit__ sent lifespan.shutdown; the wrapper drained before returning
        assert app.runtime.budget.in_flight == 0
        assert app.runtime.budget.check("x", "y") is not None  # closed

    asyncio.run(go())


# -- install ----------------------------------------------------------------


@pytest.mark.parametrize("host", ["claude-desktop", "claude-code", "cursor", "vscode"])
def test_install_renders_valid_json_for_each_host(host: str, tmp_path: Path):
    rendered, written = install(host, project_root=str(tmp_path))
    assert written is None
    document = json.loads(rendered.split("\n#", 1)[0])
    key = "servers" if host == "vscode" else "mcpServers"
    entry = document[key]["raceguard"]
    assert os.path.isabs(entry["command"])
    assert entry["args"][-2:] == ["mcp", "serve"]
    assert entry["env"]["RACEGUARD_MCP_ROOT"] == str(tmp_path.resolve())


def test_install_write_merges_into_an_existing_file(tmp_path: Path):
    target = tmp_path / ".mcp.json"
    target.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    _, written = install("claude-code", write=True, project_root=str(tmp_path))
    assert written == target
    merged = json.loads(target.read_text())
    assert set(merged["mcpServers"]) == {"other", "raceguard"}


def test_install_refuses_an_unknown_host():
    with pytest.raises(UsageError):
        install("emacs")
    with pytest.raises(UsageError):
        snippet("emacs", Path("."))


def test_log_format_resolves_after_transport_flags():
    """`--transport http` is applied after from_env; the JSON default must
    follow the final transport, not the one the environment happened to say."""
    s = MCPSettings.from_env({})
    assert s.resolved_log_format == "text"
    s.transport = "http"
    assert s.resolved_log_format == "json"
    explicit = MCPSettings.from_env(
        {"RACEGUARD_LOG_FORMAT": "text", "RACEGUARD_MCP_TRANSPORT": "http"}
    )
    assert explicit.resolved_log_format == "text"


def test_a_blindspot_warning_is_promoted_out_of_the_verdict():
    """`not reproduced` plus a buried WARNING is how an agent concludes "no
    race" on a handler that races. The warning that the invariant is measuring
    the wrong thing must reach the summary an agent actually reads."""
    from raceguard.contracts import Invariant, Verdict
    from raceguard.mcp.tools import VerifyResult

    invariant = Invariant(
        kind="non_negative",
        table="accounts",
        column="balance",
        predicate="p",
        check_sql="SELECT 1",
        source="config",
        derivation="d",
    )
    verdict = Verdict(
        finding_id="f",
        target_id="transfer",
        invariant=invariant,
        violated=False,
        violating_trials=0,
        total_trials=30,
        usable_trials=30,
        concurrency=2,
        notes=[
            "WARNING: the invariant held, but 2 requests succeeded together in a single trial",
            "amplified: a 25ms delay was inserted",
        ],
    )
    warnings = [n for n in verdict.notes if n.startswith("WARNING")]
    summary = verdict.summary()
    if warnings:
        summary = f"{summary} -- {len(warnings)} WARNING(S): read `warnings` before trusting this"
    result = VerifyResult(
        verdict=verdict.model_dump(mode="json"), summary=summary, warnings=warnings
    )

    assert result.warnings and "measuring" not in result.summary
    assert "WARNING" in result.summary, "a clean-looking summary hides the false negative"
    assert "not a proof of correctness" in result.note


def test_wildcard_bind_does_not_reject_every_client():
    """Binding 0.0.0.0 behind a published container port means the Host a
    client sends (127.0.0.1:18810) is never the bind address (0.0.0.0:8765).
    Enforcing a Host allowlist there rejects every real request, which made the
    documented `docker run` invocation unusable."""
    from mcp.server.transport_security import TransportSecuritySettings

    captured: dict = {}
    real = TransportSecuritySettings.__init__

    def spy(self, **kwargs):
        captured.update(kwargs)
        real(self, **kwargs)

    settings = _settings(host="0.0.0.0", port=8765)
    import unittest.mock as m

    with m.patch.object(TransportSecuritySettings, "__init__", spy):
        build_http_app(settings)
    assert captured["enable_dns_rebinding_protection"] is False

    # An operator who names the external host gets enforcement back.
    with m.patch.object(TransportSecuritySettings, "__init__", spy):
        build_http_app(_settings(host="0.0.0.0", allowed_hosts=["mcp.example.com"]))
    assert captured["enable_dns_rebinding_protection"] is True
    assert captured["allowed_hosts"] == ["mcp.example.com"]

    # Loopback keeps strict protection: that is the case rebinding attacks target.
    with m.patch.object(TransportSecuritySettings, "__init__", spy):
        build_http_app(_settings(host="127.0.0.1", port=9999))
    assert captured["enable_dns_rebinding_protection"] is True
    assert "127.0.0.1:*" in captured["allowed_hosts"]
