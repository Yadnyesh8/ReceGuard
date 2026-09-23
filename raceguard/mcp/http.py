"""The hosted surface: ``/mcp`` behind bearer auth, plus the endpoints a load
balancer and an operator need, unauthenticated so a probe can reach them.

Auth is a small ASGI layer in front of the SDK's Starlette app rather than the
SDK's OAuth machinery: static tokens need no issuer, no metadata endpoint and
no discovery document, and an operator who wants those plugs a different
``TokenVerifier`` into the same seam. The layer also intercepts lifespan
shutdown so in-flight verifications drain before the process goes away.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from .. import __version__ as TOOL_VERSION
from .auth import StaticTokenVerifier
from .budget import current_client
from .runtime import Runtime
from .tools import docker_reachable

log = logging.getLogger("raceguard.mcp.http")

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def register_routes(mcp, runtime: Runtime) -> None:
    """Health, readiness, metrics and version, via the SDK's unauthenticated
    ``custom_route`` so they live on the same app and share its lifespan."""
    settings = runtime.settings
    ready_cache: dict[str, Any] = {"at": 0.0, "docker": None}

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/readyz", methods=["GET"], include_in_schema=False)
    async def readyz(_: Request) -> Response:
        problems: list[str] = []
        try:
            import libcst  # noqa: F401
        except ImportError as exc:
            problems.append(f"scanner unavailable: {exc}")
        if settings.verify_enabled:
            now = time.monotonic()
            if ready_cache["docker"] is None or now - ready_cache["at"] > 30:
                ready_cache["docker"] = await asyncio.to_thread(docker_reachable)
                ready_cache["at"] = now
            if not ready_cache["docker"]:
                problems.append("docker unreachable; verify_handler would fail")
        if not settings.project_root.is_dir():
            problems.append(f"project root {settings.project_root} missing")
        body = {"status": "ready" if not problems else "not ready", "problems": problems}
        return JSONResponse(body, status_code=200 if not problems else 503)

    @mcp.custom_route("/version", methods=["GET"], include_in_schema=False)
    async def version(_: Request) -> Response:
        import mcp as sdk

        return JSONResponse(
            {
                "raceguard": TOOL_VERSION,
                "mcp_sdk": getattr(sdk, "__version__", "unknown"),
                "transport": "streamable-http",
                "verify_enabled": settings.verify_enabled,
            }
        )

    if settings.metrics_enabled:

        @mcp.custom_route("/metrics", methods=["GET"], include_in_schema=False)
        async def metrics(_: Request) -> Response:
            text = runtime.metrics.render(
                in_flight=runtime.budget.in_flight, refusals=runtime.budget.refusals
            )
            return PlainTextResponse(text, media_type="text/plain; version=0.0.4")


class HostedApp:
    """ASGI wrapper: bearer auth on the protocol path, drain on shutdown."""

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        runtime: Runtime,
        verifier: StaticTokenVerifier,
        *,
        protected_path: str = "/mcp",
    ) -> None:
        self.app = app
        self.runtime = runtime
        self.verifier = verifier
        self.protected_path = protected_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, self._draining_receive(receive), send)
            return
        if scope["type"] == "http" and scope.get("path", "").startswith(self.protected_path):
            identity = self._identify(scope)
            if identity is None:
                await _unauthorized(send)
                return
            token = current_client.set(identity)
            try:
                await self.app(scope, receive, send)
            finally:
                current_client.reset(token)
            return
        await self.app(scope, receive, send)

    def _identify(self, scope: Scope) -> str | None:
        header = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                header = value
                break
        scheme, _, credential = header.decode("latin-1").partition(" ")
        if scheme.lower() == "bearer" and credential.strip():
            access = self.verifier.verify(credential.strip())
            if access is not None:
                return access.subject or access.client_id
            return None
        if self.runtime.settings.allow_anonymous and not self.verifier.configured:
            client = scope.get("client") or ("anon", 0)
            return f"anon_{client[0]}"
        return None

    def _draining_receive(self, receive: Receive) -> Receive:
        async def wrapped() -> dict[str, Any]:
            message = await receive()
            if message.get("type") == "lifespan.shutdown":
                budget = self.runtime.budget
                budget.close()
                grace = self.runtime.settings.shutdown_grace_s
                if budget.in_flight:
                    log.info(
                        "shutdown: draining %d verification(s) for up to %ss",
                        budget.in_flight,
                        grace,
                    )
                    drained = await asyncio.to_thread(budget.drain, grace)
                    if not drained:
                        log.warning(
                            "shutdown: %d verification(s) still running after %ss",
                            budget.in_flight,
                            grace,
                        )
            return message

        return wrapped


async def _unauthorized(send: Send) -> None:
    body = json.dumps(
        {"error": "unauthorized", "hint": "send `Authorization: Bearer <token>` for /mcp"}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b'Bearer realm="raceguard"'),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
