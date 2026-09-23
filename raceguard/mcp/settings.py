"""Every knob the MCP server has, from the environment, overridable by flags.

Defaults are the safe ones: stdio, loopback, sandbox on, no anonymous HTTP.
A hosted deployment sets the environment and gets a server that refuses to
start in any configuration that would expose it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..errors import ConfigError

Transport = Literal["stdio", "http"]

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _log_format(value: str | None) -> Literal["text", "json", "auto"]:
    lowered = (value or "").strip().lower()
    if lowered in {"text", "json"}:
        return lowered  # type: ignore[return-value]
    return "auto"


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value else default
    except ValueError:
        return default


@dataclass
class MCPSettings:
    transport: Transport = "stdio"
    host: str = "127.0.0.1"
    port: int = 8765
    project_root: Path = field(default_factory=lambda: Path.cwd().resolve())
    allow_any_path: bool = False
    tokens: list[str] = field(default_factory=list)  # raw "token[:scope,scope]" entries
    allow_anonymous: bool = False
    allowed_hosts: list[str] = field(default_factory=list)
    session_cap: int = 20
    target_cap_per_hour: int = 6
    max_concurrent_verifies: int = 1
    verify_enabled: bool = True
    metrics_enabled: bool = True
    log_format: Literal["text", "json", "auto"] = "auto"  # auto: json over http, text on stdio
    default_trials: int = 10
    shutdown_grace_s: int = 30

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> MCPSettings:
        env = dict(os.environ if env is None else env)
        raw_tokens = env.get("RACEGUARD_MCP_TOKENS", "")
        tokens = [t.strip() for t in raw_tokens.split(",") if t.strip()]
        root = env.get("RACEGUARD_MCP_ROOT")
        transport = env.get("RACEGUARD_MCP_TRANSPORT", "stdio").strip().lower()
        if transport not in {"stdio", "http"}:
            raise ConfigError(
                f"RACEGUARD_MCP_TRANSPORT={transport!r} is not supported",
                hint="use stdio or http",
            )
        hosts = env.get("RACEGUARD_MCP_ALLOWED_HOSTS", "")
        return cls(
            transport=transport,  # type: ignore[arg-type]
            host=env.get("RACEGUARD_MCP_HOST", "127.0.0.1"),
            port=_int(env.get("RACEGUARD_MCP_PORT"), 8765),
            project_root=Path(root).resolve() if root else Path.cwd().resolve(),
            allow_any_path=_bool(env.get("RACEGUARD_MCP_ALLOW_ANY_PATH"), False),
            tokens=tokens,
            allow_anonymous=_bool(env.get("RACEGUARD_MCP_ALLOW_ANONYMOUS"), False),
            allowed_hosts=[h.strip() for h in hosts.split(",") if h.strip()],
            session_cap=_int(env.get("RACEGUARD_MCP_SESSION_CAP"), 20),
            target_cap_per_hour=_int(env.get("RACEGUARD_MCP_TARGET_CAP"), 6),
            max_concurrent_verifies=max(1, _int(env.get("RACEGUARD_MCP_MAX_VERIFIES"), 1)),
            verify_enabled=_bool(env.get("RACEGUARD_MCP_VERIFY"), True),
            metrics_enabled=_bool(env.get("RACEGUARD_MCP_METRICS"), True),
            log_format=_log_format(env.get("RACEGUARD_LOG_FORMAT")),
            default_trials=_int(env.get("RACEGUARD_MCP_TRIALS"), 10),
            shutdown_grace_s=_int(env.get("RACEGUARD_MCP_SHUTDOWN_GRACE"), 30),
        )

    @property
    def resolved_log_format(self) -> str:
        """Resolved at serve time, AFTER flags: a --transport http override must
        still get structured logs even though from_env saw stdio."""
        if self.log_format == "auto":
            return "json" if self.transport == "http" else "text"
        return self.log_format

    @property
    def is_loopback(self) -> bool:
        return self.host in LOOPBACK

    def validate(self) -> None:
        """Refuse configurations that would expose the server. Called at startup."""
        if not self.project_root.is_dir():
            raise ConfigError(f"project root {self.project_root} is not a directory")
        if self.transport == "http":
            if not self.tokens and not self.allow_anonymous:
                raise ConfigError(
                    "HTTP transport with no bearer tokens",
                    hint=(
                        "set RACEGUARD_MCP_TOKENS=token[:scope+scope][,token2...] or pass "
                        "--allow-anonymous for a loopback listener behind a proxy that "
                        "terminates auth"
                    ),
                )
            if self.allow_anonymous and not self.is_loopback:
                raise ConfigError(
                    f"anonymous HTTP on a non-loopback host ({self.host}) is refused",
                    hint="bind to 127.0.0.1 behind a reverse proxy, or configure tokens",
                )
            if self.allow_any_path:
                # Not an error: the flag is meaningful for a local stdio session
                # and ignored over HTTP. The log at startup says so.
                self.allow_any_path = False
