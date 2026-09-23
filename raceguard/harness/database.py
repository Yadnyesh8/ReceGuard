"""Container lifecycle for the verification harness.

Everything stateless (connect, quote, seed, snapshot, invariant check, trial
database create and drop) lives in ``raceguard.db``, which is shared
infrastructure that the synthesizer also needs. This module owns only what is
genuinely the harness's: starting the PostgreSQL container, building the
template, and tearing both down.

THE TRAP, restated here because it is where it bites: ``CREATE DATABASE ...
TEMPLATE`` requires that no other session is connected to the template. The
pool used to run the migration must be disposed before the first template
copy, or PostgreSQL raises ``source database "rg_template" is being accessed by
other users`` and every trial fails for a reason that looks nothing like its
cause. ``raceguard.db._assert_template_idle`` exists for exactly that.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from ..db import (  # noqa: F401  (re-exported for the harness's own callers)
    TEMPLATE_DB,
    Connection,
    check_invariant,
    connect,
    create_template,
    create_trial_database,
    drop_trial_database,
    quote_ident,
    seed,
    snapshot,
    to_driver_url,
    to_sync_url,
    with_database,
)
from ..errors import InfrastructureError

log = logging.getLogger("raceguard.harness.database")

__all__ = [
    "ContainerHandle",
    "start_postgres",
    "TEMPLATE_DB",
    "connect",
    "quote_ident",
    "with_database",
    "to_sync_url",
    "to_driver_url",
    "create_template",
    "create_trial_database",
    "drop_trial_database",
    "seed",
    "snapshot",
    "check_invariant",
]


@dataclass
class ContainerHandle:
    url: str
    container: Any = None
    run_id: str = ""
    started_at: float = field(default_factory=time.monotonic)
    external: bool = False

    def stop(self) -> None:
        if self.container is not None:
            try:
                self.container.stop()
            except Exception as exc:  # pragma: no cover - best effort teardown
                log.warning("could not stop container: %r", exc)


# --------------------------------------------------------------------------
# Container
# --------------------------------------------------------------------------


def start_postgres(
    image: str = "postgres:17-alpine",
    *,
    run_id: str,
    extensions: tuple[str, ...] = (),
) -> ContainerHandle:
    """Start one PostgreSQL container for this run, labelled for cleanup.

    Every container carries ``raceguard.run=<id>`` so ``raceguard clean`` is a
    label filter rather than a name guess.
    """
    external = os.environ.get("RACEGUARD_DATABASE_URL")
    if external:
        # Used by CI and by the test suite when a database already exists.
        handle = ContainerHandle(url=to_sync_url(external), run_id=run_id, external=True)
        _ensure_extensions(handle.url, extensions)
        return handle

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError as exc:  # pragma: no cover - optional extra
        raise InfrastructureError(
            "the verification harness needs the 'harness' extra",
            hint="pip install 'raceguard[harness]'",
        ) from exc

    if "@sha256:" not in image:
        log.warning(
            "database image %s is pinned by tag, not by digest; a moved tag can "
            "silently change verification results",
            image,
        )
    try:
        container = PostgresContainer(image, driver=None)
        container.with_kwargs(labels={"raceguard.run": run_id})
        container.start()
    except Exception as exc:
        raise InfrastructureError(
            f"could not start the PostgreSQL container from {image!r}: {exc}",
            hint="run `raceguard doctor` to check Docker, image pull, and ports",
        ) from exc

    url = to_sync_url(container.get_connection_url())
    handle = ContainerHandle(url=url, container=container, run_id=run_id)
    _wait_ready(url)
    _ensure_extensions(url, extensions)
    return handle


def _wait_ready(url: str, timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with connect(url) as conn:
                conn.execute("SELECT 1")
            return
        except Exception as exc:  # noqa: BLE001 - polling a starting container
            last = exc
            time.sleep(0.25)
    raise InfrastructureError(f"database never became ready within {timeout_s}s: {last!r}")


def _ensure_extensions(url: str, extensions: tuple[str, ...]) -> None:
    if not extensions:
        return
    with connect(url, autocommit=True) as conn:
        for name in extensions:
            conn.execute(f"CREATE EXTENSION IF NOT EXISTS {quote_ident(name)}")
