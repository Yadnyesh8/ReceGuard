"""The mechanical gate. Nothing reaches the harness unvalidated.

Four steps, in order (section 7.4):

1. the table and column exist in the reflected metadata,
2. ``check_sql`` parses and passes EXPLAIN against a scratch database,
3. ``check_sql`` returns ZERO rows against the seeded initial state. An
   invariant already violated before any request is fired is a bad invariant,
   not a discovered bug,
4. ``check_sql`` returns AT LEAST ONE row against a deliberately corrupted
   state the validator builds itself. This proves the check detects what it
   claims to detect, and it is the step that catches the subtle hallucination
   where the SQL is valid but tests the wrong thing.

A candidate failing any step is discarded. There is deliberately no retry loop:
a loop that eventually accepts is a loop that accepts nonsense.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import NoReturn

from ..contracts import Invariant
from ..errors import InvariantRejected
from .schema import Schema

log = logging.getLogger("raceguard.synth.validate")


@dataclass
class ValidationReport:
    passed: bool
    steps: list[tuple[int, str, bool, str]] = field(default_factory=list)

    def record(self, step: int, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append((step, name, ok, detail))

    def render(self) -> str:
        lines = []
        for step, name, ok, detail in self.steps:
            mark = "pass" if ok else "FAIL"
            lines.append(f"  {step}. {name}: {mark}{(' - ' + detail) if detail else ''}")
        return "\n".join(lines)


# --------------------------------------------------------------------------


def validate(
    invariant: Invariant,
    schema: Schema,
    *,
    admin_url: str | None = None,
    template: str = "rg_template",
    seed: dict[str, list[dict]] | None = None,
    strict: bool = True,
) -> ValidationReport:
    report = ValidationReport(passed=True)

    # -- step 1: the table and column actually exist --------------------
    table = schema.table(invariant.table)
    if table is None:
        report.passed = False
        report.record(1, "table exists", False, f"{invariant.table!r} is not in the schema")
        _fail(report, 1, f"invariant names table {invariant.table!r}, which does not exist")
    report.record(1, "table exists", True, invariant.table)

    if invariant.column:
        if not table.has(invariant.column):
            report.passed = False
            known = ", ".join(sorted(table.columns)) or "(none)"
            _fail(
                report,
                1,
                f"invariant names column {invariant.table}.{invariant.column!r}, which "
                f"does not exist. Columns on that table: {known}",
            )
        report.record(1, "column exists", True, f"{invariant.table}.{invariant.column}")

    if not invariant.check_sql.strip():
        report.passed = False
        _fail(report, 2, "the invariant carries no check_sql")

    if admin_url is None:
        # Static validation only: steps 2 to 4 need a live database. Say so
        # rather than reporting a pass that was never run.
        report.record(2, "check_sql explains", False, "skipped: no database available")
        report.record(3, "holds on the seeded state", False, "skipped: no database available")
        report.record(4, "detects a corrupted state", False, "skipped: no database available")
        return report

    from .. import db

    scratch_name, scratch_url = db.create_trial_database(admin_url, 9_000 + _salt(), template=template)
    try:
        # -- step 2: the SQL parses and plans ---------------------------
        try:
            with db.connect(scratch_url) as conn:
                conn.execute(f"EXPLAIN {invariant.check_sql}", invariant.params or {})
        except Exception as exc:  # noqa: BLE001
            report.passed = False
            _fail(report, 2, f"check_sql did not plan against the schema: {exc}")
        report.record(2, "check_sql explains", True)

        # -- step 3: it holds on the seeded initial state ---------------
        if seed:
            db.seed(scratch_url, seed)
        violated, rows = db.check_invariant(scratch_url, invariant.check_sql, invariant.params)
        if violated:
            report.passed = False
            _fail(
                report,
                3,
                f"check_sql already returns {len(rows)} row(s) against the seeded "
                f"initial state, before any request was fired. That is a bad "
                f"invariant, not a discovered bug. First violating row: {rows[0]!r}",
            )
        report.record(3, "holds on the seeded state", True, "zero rows, as required")

        # -- step 4: it detects a state that genuinely violates it -------
        corruption = build_corruption(invariant, schema)
        if corruption is None:
            report.record(
                4,
                "detects a corrupted state",
                False,
                f"no corruption recipe for kind {invariant.kind!r}; the check was not "
                f"proven to detect anything",
            )
            if strict:
                report.passed = False
                _fail(
                    report,
                    4,
                    f"cannot construct a violating state for kind {invariant.kind!r}, so "
                    f"this check could not be proven to detect what it claims",
                )
        else:
            try:
                with db.connect(scratch_url) as conn:
                    for statement, params in corruption:
                        conn.execute(statement, params)
                    conn.commit()
            except Exception as exc:  # noqa: BLE001
                report.passed = False
                _fail(report, 4, f"could not build the violating state: {exc}")
            detected, rows = db.check_invariant(
                scratch_url, invariant.check_sql, invariant.params
            )
            if not detected:
                report.passed = False
                _fail(
                    report,
                    4,
                    "check_sql returned zero rows against a state deliberately built to "
                    "violate the invariant. The SQL is valid but it is testing the "
                    "wrong thing, which is exactly the failure this step exists to "
                    "catch.",
                )
            report.record(4, "detects a corrupted state", True, f"{len(rows)} row(s)")
    finally:
        db.drop_trial_database(admin_url, scratch_name)

    return report


def _salt() -> int:
    return int(uuid.uuid4().int % 1000)


def _fail(report: ValidationReport, step: int, message: str) -> NoReturn:
    raise InvariantRejected(
        f"invariant rejected at step {step} of the validation gate: {message}",
        step=step,
        hint=(
            "the candidate is discarded rather than retried. A loop that "
            "eventually accepts is a loop that accepts nonsense."
        ),
    )


# --------------------------------------------------------------------------
# Corruption recipes: how to build a state that MUST trip each check.
# --------------------------------------------------------------------------


def build_corruption(
    invariant: Invariant, schema: Schema
) -> list[tuple[str, dict]] | None:
    """Statements that MUST make ``check_sql`` return at least one row.

    Deliberately schema aware. An earlier version used
    ``INSERT INTO t DEFAULT VALUES``, which fails the moment the table has a
    NOT NULL column with no default, and a step 4 that cannot build its own
    counterexample proves nothing about the check.
    """
    from ..db import quote_ident

    table = schema.table(invariant.table)
    if table is None:
        return None
    table_sql = quote_ident(invariant.table)

    if invariant.kind == "non_negative" and invariant.column:
        column_sql = quote_ident(invariant.column)
        row = _synthetic_row(table, overrides={invariant.column: "-1"})
        statements = [_insert(table_sql, row)]
        # Belt and braces: if the seed already put rows there, drive them
        # negative too, so the check has to notice something either way.
        statements.append((f"UPDATE {table_sql} SET {column_sql} = -1", {}))
        return statements

    if invariant.kind == "conservation" and invariant.column:
        column_sql = quote_ident(invariant.column)
        # Break the total by exactly one unit. A check that merely tests for a
        # non-empty table will not notice, which is the point of the step.
        return [(f"UPDATE {table_sql} SET {column_sql} = {column_sql} + 1", {})]

    if invariant.kind in {"single_use", "duplicate_insert", "aggregate_limit"}:
        keys = _key_columns(invariant, table)
        if not keys:
            return None
        # Two rows sharing the same key. Any GROUP BY ... HAVING COUNT(*) > n
        # formulation has to trip on this.
        limit = invariant.params.get("limit", 1)
        copies = int(limit) + 2 if isinstance(limit, (int, float)) else 3
        row = _synthetic_row(table, overrides={k: _key_value(table, k) for k in keys})
        return [_insert(table_sql, row) for _ in range(copies)]
    return None


def _insert(table_sql: str, row: dict[str, str]) -> tuple[str, dict]:
    if not row:
        return (f"INSERT INTO {table_sql} DEFAULT VALUES", {})
    from ..db import quote_ident

    columns = ", ".join(quote_ident(c) for c in row)
    values = ", ".join(row.values())
    return (f"INSERT INTO {table_sql} ({columns}) VALUES ({values})", {})


def _synthetic_row(table, overrides: dict[str, str] | None = None) -> dict[str, str]:
    """A literal for every column that must be supplied, and nothing more."""
    overrides = overrides or {}
    row: dict[str, str] = {}
    for name, column in table.columns.items():
        if name in overrides:
            row[name] = overrides[name]
            continue
        if column.nullable or column.default is not None:
            continue
        if name in table.primary_key and _is_serial(column):
            continue
        row[name] = _literal_for(column.type)
    return row


def _is_serial(column) -> bool:
    return bool(column.default and "nextval" in str(column.default))


def _key_value(table, column: str) -> str:
    found = table.columns.get(column)
    if found is None:
        return "'raceguard'"
    return _literal_for(found.type, seed="rg-dup")


def _literal_for(data_type: str, seed: str = "raceguard") -> str:
    lowered = (data_type or "").lower()
    if any(t in lowered for t in ("int", "serial")):
        return "1"
    if any(t in lowered for t in ("numeric", "decimal", "real", "double", "money", "float")):
        return "1"
    if "bool" in lowered:
        return "false"
    if "timestamp" in lowered or "date" in lowered:
        return "now()"
    if "uuid" in lowered:
        return "gen_random_uuid()"
    if "json" in lowered:
        return "'{}'::jsonb" if "jsonb" in lowered else "'{}'::json"
    return f"'{seed}'"


def _key_columns(invariant: Invariant, table) -> list[str]:
    if invariant.column and table.has(invariant.column):
        return [invariant.column]
    keyish = [
        name
        for name in table.columns
        if name not in table.primary_key
        and (name.endswith("_id") or name in {"key", "code", "email", "token"})
    ]
    return keyish[:1]
