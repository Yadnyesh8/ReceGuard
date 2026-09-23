"""Build the server, pick a transport, and shut down without leaving a
container behind."""

from __future__ import annotations

import json
import logging
import signal
import sys
import time
from typing import Any

from .. import __version__ as TOOL_VERSION
from . import http as http_surface
from . import prompts, resources, tools
from .auth import StaticTokenVerifier
from .runtime import Runtime
from .settings import MCPSettings

log = logging.getLogger("raceguard.mcp")

# `pip install mcp` resolves to 2.x, where FastMCP became MCPServer while the
# decorator API stayed the same. Both spellings are supported, resolved once.
try:  # SDK v2
    from mcp.server import MCPServer as _Server

    SDK_MAJOR = 2
except ImportError:  # pragma: no cover - SDK v1 fallback
    from mcp.server.fastmcp import (
        FastMCP as _Server,  # type: ignore[assignment,no-redef,attr-defined]
    )

    SDK_MAJOR = 1

INSTRUCTIONS = (
    "RaceGuard finds, proves and repairs read-decide-write races in Python HTTP "
    "handlers. analyze_handler is free; verify_handler boots a database and is "
    "budgeted. A finding is a suspicion; a verdict with zero violations is bounded "
    "evidence, never a proof of safety. suggest_fix returns a diff and writes nothing."
)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(settings: MCPSettings) -> None:
    # stderr always: stdout is the protocol channel on stdio, and the 2026
    # guidance retires MCP-level logging in favour of stderr and OpenTelemetry.
    handler = logging.StreamHandler(sys.stderr)
    if settings.resolved_log_format == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def build(settings: MCPSettings) -> tuple[Any, Runtime]:
    """An MCPServer with every tool, resource, prompt and route registered."""
    runtime = Runtime.from_settings(settings)
    mcp = _Server("raceguard", instructions=INSTRUCTIONS, version=TOOL_VERSION)
    tools.register(mcp, runtime)
    resources.register(mcp, runtime)
    prompts.register(mcp, runtime)
    if settings.transport == "http":
        http_surface.register_routes(mcp, runtime)
    return mcp, runtime


def build_http_app(settings: MCPSettings):
    """The ASGI app for hosting, ready for uvicorn, gunicorn or a test client."""
    from mcp.server.transport_security import TransportSecuritySettings

    mcp, runtime = build(settings)
    # DNS rebinding protection compares the request's Host header against an
    # allowlist. That is worth enforcing for a loopback listener, which is the
    # case the attack targets: a malicious page resolving a name to 127.0.0.1
    # to reach a server only the user should reach.
    #
    # On a wildcard bind it cannot work unattended. The bind address is never
    # the Host a client sends, and behind a published container port or a
    # proxy the port differs too (:8765 inside, :18810 outside), so every real
    # request is rejected. An operator who knows the external name sets
    # RACEGUARD_MCP_ALLOWED_HOSTS and gets enforcement; one who does not gets
    # the protection disabled with a warning rather than a server that refuses
    # every request. Bearer auth, not the Host header, is the control here.
    if settings.allowed_hosts:
        allowed = list(settings.allowed_hosts)
        enforce_hosts = True
    elif settings.is_loopback:
        allowed = [
            f"{settings.host}:{settings.port}",
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
        ]
        enforce_hosts = True
    else:
        allowed = []
        enforce_hosts = False
        log.warning(
            "DNS rebinding protection is off: bound to %s, which is not a name any "
            "client sends as Host. Set RACEGUARD_MCP_ALLOWED_HOSTS to the external "
            "host(s) to enforce it; bearer auth remains required.",
            settings.host,
        )
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=enforce_hosts,
        allowed_hosts=allowed,
        allowed_origins=[f"http://{h}" for h in allowed] + [f"https://{h}" for h in allowed],
    )
    inner = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=security,
        host=settings.host,
    )
    verifier = StaticTokenVerifier(settings.tokens)
    return http_surface.HostedApp(inner, runtime, verifier), runtime


def serve(settings: MCPSettings) -> None:
    settings.validate()
    configure_logging(settings)
    if settings.transport == "http":
        _serve_http(settings)
    else:
        _serve_stdio(settings)


def _serve_stdio(settings: MCPSettings) -> None:
    mcp, runtime = build(settings)
    log.info(
        "raceguard MCP %s over stdio (SDK v%s), root=%s, sandbox=%s, verify=%s",
        TOOL_VERSION,
        SDK_MAJOR,
        settings.project_root,
        "off" if settings.allow_any_path else "on",
        settings.verify_enabled,
    )
    _install_signal_handlers(runtime)
    mcp.run("stdio")


def _serve_http(settings: MCPSettings) -> None:
    import uvicorn

    app, runtime = build_http_app(settings)
    log.info(
        "raceguard MCP %s over streamable HTTP on %s:%d (SDK v%s), root=%s, auth=%s, verify=%s",
        TOOL_VERSION,
        settings.host,
        settings.port,
        SDK_MAJOR,
        settings.project_root,
        "bearer" if settings.tokens else "anonymous (loopback)",
        settings.verify_enabled,
    )
    if settings.allow_anonymous and not settings.tokens:
        log.warning("anonymous HTTP: every client shares one budget; put auth in front of this")
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
        # No uvicorn log config: its loggers propagate to the root handler
        # configure_logging installed, so every line is the same shape.
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=settings.shutdown_grace_s + 5,
    )
    # uvicorn owns SIGINT/SIGTERM and turns them into lifespan shutdown, which
    # HostedApp intercepts to drain in-flight verifications before exit.
    uvicorn.Server(config).run()


def _install_signal_handlers(runtime: Runtime) -> None:
    """stdio has no lifespan; drain on the signal instead, then exit."""

    def _handler(signum, frame):  # pragma: no cover - signal path
        runtime.budget.close()
        grace = runtime.settings.shutdown_grace_s
        if runtime.budget.in_flight:
            log.info(
                "signal %s: draining %d verification(s) for up to %ss",
                signum,
                runtime.budget.in_flight,
                grace,
            )
            # The verify runs on a worker thread whose `finally` stops its
            # container; waiting here is what lets that finally run.
            runtime.budget.drain(grace)
        raise SystemExit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):  # pragma: no cover - not the main thread
            pass


def main() -> None:
    serve(MCPSettings.from_env())


if __name__ == "__main__":
    main()
