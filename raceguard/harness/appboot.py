"""Booting the application under test, in either mode.

**container mode (default).** Build or pull the app image, run the configured
start command with the database URL environment variable pointed at the trial
database, wait for the health endpoint, fire real HTTP. This preserves worker
count, thread pool sizing, and process boundaries, which is the only way to
reproduce races that depend on more than one process.

**inprocess mode.** Import the ASGI app and drive it through
``httpx.ASGITransport``. Boot cost drops from roughly ten seconds to under one,
which is what makes the MCP editor loop usable. It cannot reproduce multi
process races and the report labels the mode on every verdict.

inprocess mode imports user code into the RaceGuard process, so it is strictly
less safe than container mode. That is printed once per run, not buried in the
docs.
"""

from __future__ import annotations

import importlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..errors import InfrastructureError, TargetConfigError

log = logging.getLogger("raceguard.harness.appboot")

INPROCESS_WARNING = (
    "inprocess mode imports the application under test into the RaceGuard "
    "process. It is strictly less safe than container mode; use container mode "
    "for any repository you do not control."
)


@dataclass
class BootedApp:
    client: httpx.AsyncClient
    base_url: str
    mode: str
    engine: Any = None  # the app's SQLAlchemy engine, when reachable
    thread_pool_size: int | None = None
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# inprocess
# --------------------------------------------------------------------------


def _reload_app_modules(module_path: str, source_root: Path) -> Any:
    """Import (or re-import) the ASGI app so it picks up the new DATABASE_URL.

    An app module that read the URL at import time keeps the old value unless
    every module under the project root is dropped from sys.modules first. A
    stale engine pointed at the previous trial's database is a silent,
    baffling failure, so this is deliberate rather than defensive.
    """
    module_name, _, attribute = module_path.partition(":")
    if not attribute:
        attribute = "app"

    root = source_root.resolve()
    package = module_name.split(".")[0]

    for name, module in list(sys.modules.items()):
        # Drop by NAME as well as by path. Dropping only what lives under the
        # current source root leaves a module of the same name imported earlier
        # from a DIFFERENT checkout sitting in sys.modules, and the harness then
        # verifies the old code while reporting on the new. That shows up as a
        # patch that "does not work" only when the run follows another one in
        # the same process, which is the worst kind of bug to chase.
        if name == package or name.startswith(f"{package}."):
            del sys.modules[name]
            continue
        origin = getattr(getattr(module, "__spec__", None), "origin", None)
        if not origin or origin in {"built-in", "frozen"}:
            continue
        try:
            if root in Path(origin).resolve().parents:
                del sys.modules[name]
        except (OSError, ValueError):
            continue

    # The path entry must lead, or an earlier checkout of the same package
    # still wins the import.
    root_str = str(root)
    if root_str in sys.path:
        sys.path.remove(root_str)
    sys.path.insert(0, root_str)

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise TargetConfigError(
            f"could not import {module_name!r} for inprocess mode: {exc}",
            hint="check app.module in raceguard.yaml, and that the project is importable",
        ) from exc
    try:
        return getattr(module, attribute)
    except AttributeError as exc:
        raise TargetConfigError(
            f"{module_name!r} has no attribute {attribute!r}",
            hint='app.module looks like "package.main:app"',
        ) from exc


def find_engine(app: Any) -> Any:
    """Locate the app's SQLAlchemy engine so the amplifier can attach to it."""
    from sqlalchemy.engine import Engine

    # A tuple of the engine classes to match, so an absent async extra simply
    # narrows what we look for rather than rebinding a class name to ().
    engine_types: tuple[type, ...] = (Engine,)
    try:
        from sqlalchemy.ext.asyncio import AsyncEngine

        engine_types = (Engine, AsyncEngine)
    except ImportError:  # pragma: no cover - sqlalchemy without the async extra
        pass

    seen: set[int] = set()
    candidates = [app, sys.modules.get(getattr(app, "__module__", "") or "")]
    for module_name, module in list(sys.modules.items()):
        if module_name.startswith(("shop", "app", "src")) or ".db" in module_name:
            candidates.append(module)

    for candidate in candidates:
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        for name in dir(candidate):
            if name.startswith("__"):
                continue
            try:
                value = getattr(candidate, name)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(value, engine_types):
                return value
    return None


def detect_thread_pool_size(app: Any) -> int | None:
    """FastAPI dispatches `def` handlers to a bounded anyio worker pool.

    If the pool is smaller than the configured concurrency, requests queue and
    the race cannot occur, producing a false negative. Reading the limiter is
    how the harness knows to warn.
    """
    try:
        import anyio.to_thread

        limiter = anyio.to_thread.current_default_thread_limiter()
        return int(limiter.total_tokens)
    except Exception:  # noqa: BLE001 - not running in an anyio context yet
        try:
            from starlette.concurrency import _default_thread_limiter  # type: ignore

            return int(_default_thread_limiter.total_tokens)
        except Exception:  # noqa: BLE001
            return None


@asynccontextmanager
async def boot_inprocess(
    *,
    module_path: str,
    database_url: str,
    database_url_env: str,
    source_root: Path,
    env: dict[str, str] | None = None,
    concurrency: int = 8,
    timeout_s: float = 30.0,
) -> AsyncIterator[BootedApp]:
    previous = dict(os.environ)
    os.environ[database_url_env] = database_url
    os.environ.update(env or {})
    root = str(source_root.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)

    try:
        app = _reload_app_modules(module_path, source_root)
        transport = httpx.ASGITransport(app=app)
        limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
        client = httpx.AsyncClient(
            transport=transport,
            base_url="http://raceguard.test",
            limits=limits,
            timeout=timeout_s,
        )
        booted = BootedApp(
            client=client,
            base_url="http://raceguard.test",
            mode="inprocess",
            engine=find_engine(app),
            thread_pool_size=detect_thread_pool_size(app),
            notes=[INPROCESS_WARNING],
        )
        if booted.thread_pool_size and concurrency > booted.thread_pool_size:
            booted.notes.append(
                f"concurrency {concurrency} exceeds the application's thread pool "
                f"({booted.thread_pool_size}); `def` handlers will queue behind the "
                f"pool instead of arriving together, which can hide a real race"
            )
        try:
            yield booted
        finally:
            await client.aclose()
    finally:
        os.environ.clear()
        os.environ.update(previous)


# --------------------------------------------------------------------------
# container
# --------------------------------------------------------------------------


def build_app_image(dockerfile: str, context_dir: Path, run_id: str) -> str:
    """Build the application image once for the whole run.

    Building inside each trial and removing the image afterwards turns a ten
    second cost into ten seconds times the trial count, and throws away the
    layer cache that would have made the rebuild cheap.
    """
    with _built_image(dockerfile, context_dir, run_id, keep=True) as tag:
        return tag


def remove_app_image(tag: str) -> None:
    subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, text=True)


@contextmanager
def _built_image(
    dockerfile: str, context_dir: Path, run_id: str, keep: bool = False
) -> Iterator[str]:
    tag = f"raceguard-app:{run_id[:12]}"
    # `-f` resolves against the CURRENT working directory, not against the build
    # context, and RaceGuard is routinely run from a directory above the app.
    # Resolving it against the project root is what makes `dockerfile:
    # "Dockerfile"` in raceguard.yaml mean what the user obviously intends.
    path = Path(dockerfile)
    if not path.is_absolute():
        path = context_dir / path
    if not path.is_file():
        raise InfrastructureError(
            f"dockerfile {dockerfile!r} not found at {path}",
            hint="app.dockerfile is relative to the directory holding raceguard.yaml",
        )
    command = [
        "docker", "build", "-f", str(path), "-t", tag,
        "--label", f"raceguard.run={run_id}", str(context_dir),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        raise InfrastructureError(
            f"docker build failed:\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
        )
    try:
        yield tag
    finally:
        if not keep:
            remove_app_image(tag)


@asynccontextmanager
async def boot_container(
    *,
    image: str | None,
    dockerfile: str | None,
    start_command: str | None,
    database_url: str,
    database_url_env: str,
    source_root: Path,
    health_path: str,
    run_id: str,
    env: dict[str, str] | None = None,
    concurrency: int = 8,
    amplify_table: str | None = None,
    amplify_ms: int = 0,
    timeout_s: float = 60.0,
    port: int = 8000,
) -> AsyncIterator[BootedApp]:
    """Run the app in its own container, reachable on a published port.

    Security posture (section 15): capabilities dropped, read only root
    filesystem with a writable tmpfs, memory and CPU quotas, the source
    directory mounted read only, and a wall clock kill.
    """
    from .amplify import sitecustomize_source

    if not image and not dockerfile:
        raise TargetConfigError(
            "container mode needs app.dockerfile or a prebuilt image",
            hint="set app.dockerfile in raceguard.yaml, or use mode: inprocess",
        )

    stack: list[Any] = []
    # The shim goes in its own temporary directory mounted at /rgshim, NOT into
    # the project. Mounting the host source over /app would shadow the code the
    # image already contains, and writing into the user's tree during a
    # verification run is not something a read-only analysis tool should do.
    shim_dir = Path(tempfile.mkdtemp(prefix="raceguard-shim-")) if amplify_ms > 0 else None
    if shim_dir is not None:
        (shim_dir / "sitecustomize.py").write_text(
            sitecustomize_source(amplify_table, amplify_ms), encoding="utf-8"
        )

    try:
        if image is None and dockerfile:
            # Only reached when no image was supplied. The run normally builds
            # once up front (see build_app_image) because rebuilding per trial
            # multiplies a ten second cost by the trial count for no benefit.
            builder = _built_image(dockerfile, source_root, run_id)
            image = builder.__enter__()
            stack.append(builder)

        container_name = f"raceguard-app-{run_id[:12]}"
        # `--network host` behaves as documented only on Linux. On Docker
        # Desktop it silently gives the container its own stack, so the
        # published port is unreachable and every request fails with a
        # connection error that looks nothing like its cause. Publishing the
        # port and rewriting the database host works on both.
        container_url = _reachable_from_container(database_url)
        command = [
            "docker", "run", "--rm", "-d",
            "--name", container_name,
            "--label", f"raceguard.run={run_id}",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--memory", "1g",
            "--cpus", "2",
            "-p", f"{port}:{port}",
            "--add-host", "host.docker.internal:host-gateway",
            "-e", f"{database_url_env}={container_url}",
        ]
        if shim_dir is not None:
            command += [
                "-v", f"{shim_dir}:/rgshim:ro",
                "-e", "PYTHONPATH=/rgshim",
            ]
        for key, value in (env or {}).items():
            command += ["-e", f"{key}={value}"]
        command.append(image)  # type: ignore[arg-type]
        if start_command:
            command += start_command.split()

        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise InfrastructureError(
                f"could not start the application container:\n{result.stderr[-2000:]}"
            )
        container_id = result.stdout.strip()

        base_url = f"http://127.0.0.1:{port}"
        limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
        client = httpx.AsyncClient(base_url=base_url, limits=limits, timeout=timeout_s)
        try:
            await _wait_healthy(client, health_path, timeout_s, container_id)
            yield BootedApp(
                client=client,
                base_url=base_url,
                mode="container",
                notes=[] if amplify_ms <= 0 else ["amplification shim injected via PYTHONPATH"],
            )
        finally:
            await client.aclose()
            subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)
    finally:
        for entered in reversed(stack):
            entered.__exit__(None, None, None)
        if shim_dir is not None:
            shutil.rmtree(shim_dir, ignore_errors=True)


def _reachable_from_container(url: str) -> str:
    """Rewrite a loopback database host to one the container can actually reach.

    The URL RaceGuard holds points at a port published on the host. Inside a
    container, `localhost` is the container itself, so the app would try to
    connect to a database that is not there.
    """
    for host in ("localhost", "127.0.0.1", "0.0.0.0"):
        if f"@{host}:" in url:
            return url.replace(f"@{host}:", "@host.docker.internal:", 1)
    return url


async def _wait_healthy(
    client: httpx.AsyncClient, health_path: str, timeout_s: float, container_id: str
) -> None:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            response = await client.get(health_path)
            if response.status_code < 500:
                return
            last = f"health returned {response.status_code}"
        except Exception as exc:  # noqa: BLE001 - polling a starting container
            last = repr(exc)
        await _sleep(0.25)
    logs = subprocess.run(
        ["docker", "logs", "--tail", "50", container_id], capture_output=True, text=True
    )
    raise InfrastructureError(
        f"application never became healthy at {health_path} within {timeout_s}s ({last})",
        hint=f"container logs:\n{logs.stdout[-2000:]}{logs.stderr[-2000:]}",
    )


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
