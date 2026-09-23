"""Low level PostgreSQL access, shared by the harness and the synthesizer.

This is infrastructure, not engine logic. It sits beside ``contracts``,
``config`` and ``errors`` in the import graph precisely so that ``synth`` can
run its validation gate against a scratch database without importing the
harness, which would make two engine packages depend on each other and start
the drift the architecture rule exists to prevent.

Container lifecycle lives in ``harness/database.py``. Everything here is
stateless: connect, quote, seed, snapshot, check.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from .errors import InfrastructureError, TargetConfigError

log = logging.getLogger("raceguard.db")

TEMPLATE_DB = "rg_template"
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_ident(name: str) -> str:
    """Database and table names reach SQL as identifiers, never as parameters."""
    if not _IDENT_RE.match(name):
        raise TargetConfigError(
            f"{name!r} is not a valid SQL identifier",
            hint="table and database names must match [A-Za-z_][A-Za-z0-9_]*",
        )
    return '"' + name.replace('"', '""') + '"'


def with_database(url: str, database: str) -> str:
    parts = urlparse(url)
    return urlunparse(parts._replace(path=f"/{database}"))


def to_driver_url(url: str, driver: str | None) -> str:
    """Render the URL in the dialect the application under test expects."""
    base = to_sync_url(url)
    if not driver:
        return base
    return base.replace("postgresql://", f"postgresql+{driver}://", 1)


def to_sync_url(url: str) -> str:
    """asyncpg / aiopg URLs are not usable from the synchronous admin path."""
    for prefix, replacement in (
        ("postgresql+asyncpg://", "postgresql://"),
        ("postgresql+psycopg://", "postgresql://"),
        ("postgres://", "postgresql://"),
    ):
        if url.startswith(prefix):
            return replacement + url[len(prefix) :]
    return url


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------


class Connection:
    """A thin cursor wrapper so the rest of the harness does not care which
    driver is installed."""

    def __init__(self, raw) -> None:
        self.raw = raw

    def execute(self, sql: str, params: tuple | dict | None = None) -> list[tuple]:
        with self.raw.cursor() as cur:
            cur.execute(sql, params or ())
            if cur.description is None:
                return []
            return list(cur.fetchall())

    def columns(self, sql: str, params: tuple | dict | None = None) -> list[str]:
        with self.raw.cursor() as cur:
            cur.execute(sql, params or ())
            return [d[0] for d in (cur.description or [])]

    def commit(self) -> None:
        self.raw.commit()


@contextmanager
def connect(url: str, *, autocommit: bool = False) -> Iterator[Connection]:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - optional extra
        raise InfrastructureError(
            "psycopg is required to talk to PostgreSQL",
            hint="pip install 'raceguard[harness]'",
        ) from exc
    conn = psycopg.connect(to_sync_url(url), autocommit=autocommit)
    try:
        yield Connection(conn)
        if not autocommit:
            conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Template and trial databases
# --------------------------------------------------------------------------


def create_template(
    admin_url: str,
    *,
    migrate_command: str | None,
    cwd: str,
    database_url_env: str,
    env: dict[str, str] | None = None,
    template: str = TEMPLATE_DB,
    timeout_s: int = 300,
    driver: str | None = None,
) -> str:
    """Create the template database and run migrations against it exactly once."""
    with connect(admin_url, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {quote_ident(template)}")
        conn.execute(f"CREATE DATABASE {quote_ident(template)}")

    template_url = with_database(admin_url, template)

    if migrate_command:
        environment = {
            **os.environ,
            **(env or {}),
            database_url_env: to_driver_url(template_url, driver),
        }
        # The migration runs through a plain shell, which does not inherit the
        # virtualenv RaceGuard itself was launched from. Without this, a
        # perfectly correct `alembic upgrade head` fails with "command not
        # found" and the error points at the config rather than at PATH.
        environment["PATH"] = os.pathsep.join(
            [str(Path(sys.prefix) / "bin"), environment.get("PATH", "")]
        )
        try:
            result = subprocess.run(
                migrate_command,
                shell=True,
                cwd=cwd,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise InfrastructureError(
                f"migration command timed out after {timeout_s}s: {migrate_command!r}"
            ) from exc
        if result.returncode != 0:
            raise InfrastructureError(
                f"migration command failed with exit code {result.returncode}: "
                f"{migrate_command!r}\n{result.stdout}\n{result.stderr}",
                hint="check app.migrate in raceguard.yaml",
            )

    # THE TRAP. Anything still connected to the template makes every
    # subsequent CREATE DATABASE ... TEMPLATE fail, and the error names the
    # template rather than the migration that left the connection open.
    _disconnect_all(admin_url, template)
    _assert_template_idle(admin_url, template)
    return template_url


def _disconnect_all(admin_url: str, database: str) -> None:
    with connect(admin_url, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (database,),
        )


def _assert_template_idle(admin_url: str, template: str) -> None:
    with connect(admin_url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (template,),
        )
    open_sessions = rows[0][0] if rows else 0
    if open_sessions:
        raise InfrastructureError(
            f"{open_sessions} session(s) are still connected to {template!r}, so "
            f"CREATE DATABASE ... TEMPLATE will fail",
            hint=(
                "dispose the SQLAlchemy engine or connection pool used for the "
                "migration before the first trial; this is the most common bug in "
                "the harness and it does not look like its own cause"
            ),
        )


def create_trial_database(
    admin_url: str, index: int, *, template: str = TEMPLATE_DB
) -> tuple[str, str]:
    """CREATE DATABASE rg_trial_n TEMPLATE rg_template.

    Template copy is dramatically faster than re-running migrations and,
    unlike truncation, it resets sequences and leaves constraints and
    extensions intact.
    """
    name = f"rg_trial_{index}_{uuid.uuid4().hex[:6]}"
    with connect(admin_url, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {quote_ident(name)} TEMPLATE {quote_ident(template)}")
    return name, with_database(admin_url, name)


def apply_ddl(url: str, statement: str) -> None:
    """Run one migration statement against a database, in autocommit.

    Used to bring the reverify template up to the schema a patch requires
    before its trials clone from it. Autocommit is mandatory: a patch migration
    can carry ``CREATE UNIQUE INDEX CONCURRENTLY``, which PostgreSQL refuses to
    run inside a transaction block.
    """
    if not statement or not statement.strip():
        return
    with connect(url, autocommit=True) as conn:
        conn.execute(statement)


def drop_trial_database(admin_url: str, name: str) -> None:
    try:
        _disconnect_all(admin_url, name)
        with connect(admin_url, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {quote_ident(name)}")
    except Exception as exc:  # pragma: no cover - teardown is best effort
        log.warning("could not drop trial database %s: %r", name, exc)


# --------------------------------------------------------------------------
# Seeding and snapshots
# --------------------------------------------------------------------------


def seed(url: str, rows_by_table: dict[str, list[dict]]) -> None:
    """Insert the target's boundary state.

    Seeding philosophy (section 8.1): use the tightest state that can expose
    the invariant. ``stock = 1`` with concurrency 2 is a far better signal than
    ``stock = 100`` with concurrency 50, because the final state distinguishes
    a violation cleanly rather than burying it in noise.
    """
    if not rows_by_table:
        return
    with connect(url) as conn:
        for table, rows in rows_by_table.items():
            table_sql = quote_ident(table)
            for row in rows:
                if not row:
                    continue
                columns = list(row)
                placeholders = ", ".join(["%s"] * len(columns))
                column_sql = ", ".join(quote_ident(c) for c in columns)
                conn.execute(
                    f"INSERT INTO {table_sql} ({column_sql}) VALUES ({placeholders})",
                    tuple(row[c] for c in columns),
                )
        # Sequences copied from the template still start at 1, so an explicit
        # id in the seed collides with the first row the app inserts.
        for table in rows_by_table:
            _resync_sequence(conn, table)
        conn.commit()


def _resync_sequence(conn: Connection, table: str) -> None:
    try:
        conn.execute(
            "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
            "GREATEST((SELECT COALESCE(MAX(id), 0) FROM "
            + quote_ident(table)
            + "), 1)) WHERE pg_get_serial_sequence(%s, 'id') IS NOT NULL",
            (table, table),
        )
    except Exception:  # noqa: BLE001 - table may have no id column at all
        pass


def snapshot(url: str, tables: list[str], limit: int = 50) -> dict:
    """Final state of the involved tables, for the trial record."""
    out: dict = {}
    if not tables:
        return out
    try:
        with connect(url) as conn:
            for table in tables:
                try:
                    sql = f"SELECT * FROM {quote_ident(table)} ORDER BY 1 LIMIT {int(limit)}"
                    columns = conn.columns(sql)
                    rows = conn.execute(sql)
                    out[table] = [dict(zip(columns, row, strict=False)) for row in rows]
                except Exception as exc:  # noqa: BLE001 - table may not exist
                    out[table] = {"error": repr(exc)}
    except Exception as exc:  # noqa: BLE001
        out["_error"] = repr(exc)
    return _jsonable(out)


def check_invariant(url: str, check_sql: str, params: dict | None = None) -> tuple[bool, list]:
    """Run the invariant check. Rows returned means VIOLATED. One convention."""
    with connect(url) as conn:
        rows = conn.execute(check_sql, params or {})
    return (len(rows) > 0), _jsonable(rows)


def _jsonable(value):
    from datetime import date, datetime
    from decimal import Decimal

    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value
