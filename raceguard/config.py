"""raceguard.yaml: load, validate, resolve environment variables.

Environment interpolation resolves at load time and the resolved values never
enter run artifacts (section 15). ``Config.redacted()`` is what the artifact
writer serialises.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .contracts import HygieneRule, InvariantKind, Mode, SyncStrategy
from .errors import ConfigError

CONFIG_FILENAME = "raceguard.yaml"
SCHEMA_VERSION = 1

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

SECRET_KEY_PATTERN = re.compile(
    r"(authorization|api[-_]?key|secret|token|password|passwd|cookie|bearer)",
    re.IGNORECASE,
)


def _interpolate(value: Any, *, missing: list[str]) -> Any:
    """Replace ``${VAR}`` and ``${VAR:-default}`` from the environment."""
    if isinstance(value, str):

        def repl(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            env = os.environ.get(name)
            if env is not None:
                return env
            if default is not None:
                return default
            missing.append(name)
            return ""

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v, missing=missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, missing=missing) for v in value]
    return value


def redact(value: Any, key: str | None = None) -> Any:
    """Strip anything that looks like a credential before it reaches an artifact."""
    if key is not None and SECRET_KEY_PATTERN.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str) and value.lower().startswith("bearer "):
        return "<redacted>"
    return value


# --------------------------------------------------------------------------


class AppConfig(BaseModel):
    module: str | None = None  # "shop.main:app", for inprocess mode
    start: str | None = None  # process command, for container mode
    dockerfile: str | None = None
    health_path: str = "/health"
    base_url: str = "http://localhost:8000"
    database_url_env: str = "DATABASE_URL"
    # The SQLAlchemy driver the application expects in its URL, e.g. "asyncpg"
    # or "psycopg". Left unset the URL is handed over as plain
    # postgresql://..., which is what a synchronous app wants. Guessing this
    # is worse than asking: an async app handed a sync URL fails at import
    # with an error that names psycopg2 and points nowhere useful.
    database_url_driver: str | None = None
    migrate: str | None = None
    test_command: str | None = None
    thread_pool_size: int = 40  # informational, used to warn on concurrency
    source_root: str = "."
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _need_a_boot_path(self) -> AppConfig:
        if not self.module and not self.start and not self.dockerfile:
            raise ValueError(
                "app needs at least one of module (inprocess), start, or dockerfile"
            )
        return self


class DatabaseConfig(BaseModel):
    image: str = "postgres:17-alpine"
    extensions: list[str] = Field(default_factory=list)
    template_name: str = "rg_template"

    @field_validator("image")
    @classmethod
    def _warn_unpinned(cls, v: str) -> str:
        # Pinning by digest is the documented posture; a moved tag silently
        # changes results. We do not hard fail because it blocks first use.
        return v

    @property
    def pinned_by_digest(self) -> bool:
        return "@sha256:" in self.image


class Defaults(BaseModel):
    mode: Mode = "container"
    trials: int = 20
    concurrency: int = 8
    sync: SyncStrategy = "barrier"
    amplify: bool = True
    amplify_ms: int = 25
    trial_timeout_s: int = 30
    target_budget_s: int = 300
    run_budget_s: int = 1800

    @field_validator("trials", "concurrency")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("must be >= 1")
        return v


class GovernorConfig(BaseModel):
    max_global_concurrency: int = 16
    min_trial_spacing_ms: int = 250
    max_requests_per_run: int = 5000
    respect_rate_limit_headers: bool = True
    refill_per_second: float = 20.0
    burst_capacity: float = 40.0
    allow_external_target: bool = False
    external_concurrency_ceiling: int = 4


class InvariantSpec(BaseModel):
    """The config-authored invariant. Source 1, beats everything inferred."""

    kind: InvariantKind
    table: str
    column: str | None = None
    predicate: str | None = None
    check_sql: str | None = None
    params: dict = Field(default_factory=dict)
    key_columns: list[str] = Field(default_factory=list)
    expected_total: float | None = None
    limit: int | None = None


class GroupMember(BaseModel):
    route: str
    payload: dict = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)


class Target(BaseModel):
    id: str
    route: str | None = None
    handler: str | None = None  # "shop/routes/orders.py::create_order"
    spec: str | None = None  # natural language, feeds the synthesizer
    payload: dict = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    seed: dict[str, list[dict]] = Field(default_factory=dict)
    invariant: InvariantSpec | None = None
    group: list[GroupMember] = Field(default_factory=list)
    concurrency: int | None = None
    trials: int | None = None
    mode: Mode | None = None
    sync: SyncStrategy | None = None
    amplify: bool | None = None
    amplify_ms: int | None = None
    amplify_table: str | None = None
    finding_id: str | None = None

    @model_validator(mode="after")
    def _route_or_group(self) -> Target:
        if not self.route and not self.group:
            raise ValueError(f"target '{self.id}': needs a route or a group")
        if self.route and self.group:
            raise ValueError(f"target '{self.id}': route and group are mutually exclusive")
        return self

    def method_and_path(self) -> tuple[str, str]:
        if not self.route:
            raise ConfigError(f"target '{self.id}' has no single route (it is a group)")
        parts = self.route.split(None, 1)
        if len(parts) != 2:
            raise ConfigError(
                f"target '{self.id}': route must look like 'POST /orders', got {self.route!r}"
            )
        return parts[0].upper(), parts[1]


class HygieneConfig(BaseModel):
    enabled: bool = True
    rules: list[HygieneRule] = Field(
        default_factory=lambda: cast(
            "list[HygieneRule]",
            [
                "missing_idempotency_key",
                "unsafe_idempotency_check",
                "missing_rate_limit",
                "missing_optimistic_concurrency",
                "unbounded_retry",
                "swallowed_serialization_failure",
            ],
        )
    )


class Suppression(BaseModel):
    handler: str | None = None
    file: str | None = None
    rule: str
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("a suppression needs a free text reason; blank is not allowed")
        return v


class SynthConfig(BaseModel):
    """Which model, if any, backs the proposer and critic.

    The tool must be usable with no model configured at all: schema derived
    invariants alone give a working experience (section 22, question 4).
    """

    provider: Literal["none", "claude", "openai", "command"] = "none"
    model: str | None = None
    command: str | None = None  # stdin JSON -> stdout JSON, for air-gapped use
    critic_enabled: bool = True
    timeout_s: int = 60


class Config(BaseModel):
    version: int = SCHEMA_VERSION
    app: AppConfig
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    defaults: Defaults = Field(default_factory=Defaults)
    governor: GovernorConfig = Field(default_factory=GovernorConfig)
    synth: SynthConfig = Field(default_factory=SynthConfig)
    targets: list[Target] = Field(default_factory=list)
    hygiene: HygieneConfig = Field(default_factory=HygieneConfig)
    suppress: list[Suppression] = Field(default_factory=list)

    # not serialised back out
    path: str | None = None

    @field_validator("version")
    @classmethod
    def _known_version(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(
                f"config schema version {v} is not supported by this release "
                f"(expected {SCHEMA_VERSION})"
            )
        return v

    @model_validator(mode="after")
    def _unique_target_ids(self) -> Config:
        seen: set[str] = set()
        for t in self.targets:
            if t.id in seen:
                raise ValueError(f"duplicate target id {t.id!r}")
            seen.add(t.id)
        return self

    @property
    def root(self) -> Path:
        return Path(self.path).parent if self.path else Path.cwd()

    def target(self, target_id: str) -> Target:
        for t in self.targets:
            if t.id == target_id:
                return t
        known = ", ".join(t.id for t in self.targets) or "(none defined)"
        raise ConfigError(
            f"no target with id {target_id!r}", hint=f"known targets: {known}"
        )

    def resolved(self, target: Target) -> dict[str, Any]:
        """Merge target overrides over defaults into one flat settings dict."""
        d = self.defaults
        return {
            "mode": target.mode or d.mode,
            "trials": target.trials or d.trials,
            "concurrency": target.concurrency or d.concurrency,
            "sync": target.sync or d.sync,
            "amplify": d.amplify if target.amplify is None else target.amplify,
            "amplify_ms": target.amplify_ms or d.amplify_ms,
            "trial_timeout_s": d.trial_timeout_s,
            "target_budget_s": d.target_budget_s,
        }

    def redacted(self) -> dict:
        """Artifact-safe view: no resolved secrets, no Authorization values."""
        return redact(self.model_dump(mode="json", exclude={"path"}))


# --------------------------------------------------------------------------


def find_config(start: Path | str = ".") -> Path | None:
    """Walk up from ``start`` looking for raceguard.yaml."""
    here = Path(start).resolve()
    if here.is_file():
        here = here.parent
    for directory in [here, *here.parents]:
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_config(path: Path | str | None = None, *, start: Path | str = ".") -> Config:
    resolved = Path(path) if path else find_config(start)
    if resolved is None:
        raise ConfigError(
            f"no {CONFIG_FILENAME} found",
            hint="run `raceguard init` to generate one from a scan",
        )
    if not resolved.is_file():
        raise ConfigError(f"{resolved} does not exist")

    try:
        raw = yaml.safe_load(resolved.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{resolved} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{resolved} must contain a mapping at the top level")

    missing: list[str] = []
    raw = _interpolate(raw, missing=missing)
    if missing:
        names = ", ".join(sorted(set(missing)))
        raise ConfigError(
            f"{resolved} references undefined environment variables: {names}",
            hint="export them, or give a default with ${VAR:-fallback}",
        )

    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{resolved} is invalid:\n{_format_errors(exc)}") from exc
    config.path = str(resolved)
    return config


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"  {loc}: {err['msg']}")
    return "\n".join(lines)


def dump_config(config: Config) -> str:
    data = config.model_dump(mode="json", exclude={"path"}, exclude_none=True)
    return yaml.safe_dump(data, sort_keys=False, width=100)
