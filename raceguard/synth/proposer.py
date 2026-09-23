"""Candidate invariant generation.

Three sources, tried in order, with the winner recorded in ``Invariant.source``
and the reason in ``Invariant.derivation``:

1. **config** - stated in raceguard.yaml. Used verbatim. Human authored beats
   inferred, permanently.
2. **spec** - a natural language sentence on the target or in the handler's
   docstring, sent to a model along with the reflected schema.
3. **schema** - reflection alone.

The tool must be usable with NO model configured at all. Schema derived
invariants give a working experience on their own; spec derivation is an
enhancement, not a dependency. With ``synth.provider: none`` the proposer falls
back to a deterministic keyword reading of the spec sentence, which is honest
about being a heuristic in the derivation string it writes.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass

from ..config import SynthConfig
from ..contracts import Invariant, InvariantKind
from .schema import Schema

log = logging.getLogger("raceguard.synth.proposer")

# Versioned so evaluation results stay reproducible. Bump on any edit.
PROMPT_VERSION = "invariant-proposer/v1"

PROPOSER_PROMPT = """\
You derive a database invariant from a requirement sentence and a schema.

Return exactly one JSON object and no prose. Its shape:

  {{"kind": one of ["non_negative","conservation","single_use",
                    "duplicate_insert","aggregate_limit"],
    "table": string,
    "column": string or null,
    "predicate": short human readable statement,
    "check_sql": SQL that returns ZERO rows when the invariant HOLDS and at
                 least one row when it is VIOLATED,
    "derivation": one sentence naming the part of the requirement you used}}

Rules:
- Only reference tables and columns present in the schema below. Naming
  anything else is a hard failure.
- check_sql returns rows ONLY on violation. This is inverted from how such a
  rule is usually written; get it the right way round.
- No parameters other than the ones you declare, and no DDL.

REQUIREMENT
{spec}

SCHEMA
{schema}

DECISION SITE
{decision}
"""

# Keyword readings, used when no model is configured. Deliberately conservative.
_KEYWORDS: list[tuple[re.Pattern[str], InvariantKind]] = [
    (re.compile(r"\bnever\b.*\b(below|under|negative|less than zero)\b", re.I), "non_negative"),
    (
        re.compile(r"\b(oversell|out of stock|insufficient|overdraw|overdraft)\b", re.I),
        "non_negative",
    ),
    (
        re.compile(
            r"\b(total|sum|conserv|balance).{0,40}\b(unchanged|constant|preserved|must not change)\b",
            re.I,
        ),
        "conservation",
    ),
    (re.compile(r"\b(transfer|move).{0,30}\bbetween\b", re.I), "conservation"),
    (
        re.compile(
            r"\b(at most once|only once|exactly once|single use|one time|redeemed once)\b", re.I
        ),
        "single_use",
    ),
    (
        re.compile(
            r"\b(only be registered once|unique|no duplicate|already exists|create if)\b", re.I
        ),
        "duplicate_insert",
    ),
    (re.compile(r"\b(at most|no more than|maximum of|up to)\s+\w+\s+\w+", re.I), "aggregate_limit"),
]


@dataclass
class Candidate:
    invariant: Invariant
    raw: str = ""
    used_model: bool = False


# --------------------------------------------------------------------------
# check_sql templates, one per kind. These are the shapes section 2.2 defines.
# --------------------------------------------------------------------------


def check_sql_for(
    kind: InvariantKind,
    table: str,
    column: str | None,
    *,
    key_columns: list[str] | None = None,
    expected_total: float | None = None,
    limit: int | None = None,
    limit_column: str | None = None,
    group_by: str | None = None,
) -> tuple[str, dict, str]:
    """Returns (sql, params, human predicate). Rows mean VIOLATED, always."""
    from ..db import quote_ident

    t = quote_ident(table)

    if kind == "non_negative":
        c = quote_ident(column or "amount")
        return (
            f"SELECT * FROM {t} WHERE {c} < 0",
            {},
            f"{table}.{column} >= 0",
        )

    if kind == "conservation":
        c = quote_ident(column or "amount")
        total = expected_total if expected_total is not None else 0
        return (
            f"SELECT SUM({c}) AS total FROM {t} HAVING SUM({c}) <> %(expected)s",
            {"expected": total},
            f"SUM({table}.{column}) = {total}",
        )

    if kind in {"single_use", "duplicate_insert"}:
        keys = key_columns or ([column] if column else [])
        if not keys:
            raise ValueError(f"{kind} needs at least one key column")
        key_sql = ", ".join(quote_ident(k) for k in keys)
        return (
            f"SELECT {key_sql}, COUNT(*) AS n FROM {t} GROUP BY {key_sql} HAVING COUNT(*) > 1",
            {},
            f"at most one {table} row per ({', '.join(keys)})",
        )

    if kind == "aggregate_limit":
        group = group_by or (key_columns[0] if key_columns else column)
        if not group:
            raise ValueError("aggregate_limit needs a grouping column")
        g = quote_ident(group)
        if limit_column:
            # The cap lives in a column on another row, the usual shape for
            # "at most teams.max_members members per team".
            return (
                f"SELECT {g}, COUNT(*) AS n FROM {t} GROUP BY {g} HAVING COUNT(*) > %(limit)s",
                {"limit": limit if limit is not None else 1},
                f"at most {limit} {table} rows per {group}",
            )
        return (
            f"SELECT {g}, COUNT(*) AS n FROM {t} GROUP BY {g} HAVING COUNT(*) > %(limit)s",
            {"limit": limit if limit is not None else 1},
            f"at most {limit if limit is not None else 1} {table} rows per {group}",
        )

    raise ValueError(f"unknown invariant kind {kind!r}")


# --------------------------------------------------------------------------
# Source 2: spec text
# --------------------------------------------------------------------------


def propose_from_spec(
    spec: str,
    schema: Schema,
    tables: list[str],
    *,
    settings: SynthConfig,
    decision_snippet: str = "",
) -> Candidate | None:
    if not spec.strip():
        return None
    described = schema.describe(tables) or schema.describe(list(schema.tables))

    if settings.provider != "none":
        prompt = PROPOSER_PROMPT.format(
            spec=spec.strip(), schema=described, decision=decision_snippet or "(not supplied)"
        )
        raw = _call_model(prompt, settings)
        if raw:
            candidate = _parse_candidate(raw, schema)
            if candidate is not None:
                candidate.invariant.derivation = (
                    f"derived from the spec sentence {spec.strip()!r} by "
                    f"{PROMPT_VERSION} against the reflected schema"
                )
                candidate.used_model = True
                return candidate
            log.warning("proposer returned unusable output; falling back to keywords")

    return _keyword_candidate(spec, schema, tables)


def _keyword_candidate(spec: str, schema: Schema, tables: list[str]) -> Candidate | None:
    kind: InvariantKind | None = None
    matched = ""
    for pattern, candidate_kind in _KEYWORDS:
        found = pattern.search(spec)
        if found:
            kind, matched = candidate_kind, found.group(0)
            break
    if kind is None:
        return None

    table_name = _pick_table(schema, tables)
    if table_name is None:
        return None
    table = schema.table(table_name)
    if table is None:
        return None

    column = _pick_column(table, kind)
    keys = [column] if column else []
    limit = _extract_limit(spec)
    try:
        sql, params, predicate = check_sql_for(
            kind, table_name, column, key_columns=keys, limit=limit
        )
    except ValueError:
        return None

    return Candidate(
        invariant=Invariant(
            kind=kind,
            table=table_name,
            column=column,
            predicate=predicate,
            check_sql=sql,
            params=params,
            source="spec",
            derivation=(
                f"read from the spec sentence {spec.strip()!r} by keyword match on "
                f"{matched!r} with no model configured; confirm this in raceguard.yaml"
            ),
        )
    )


def _extract_limit(spec: str) -> int | None:
    words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
    }
    match = re.search(r"\b(?:at most|no more than|maximum of|up to)\s+(\w+)", spec, re.I)
    if not match:
        return None
    token = match.group(1).lower()
    if token.isdigit():
        return int(token)
    return words.get(token)


def _pick_table(schema: Schema, tables: list[str]) -> str | None:
    for name in tables:
        if name in schema.tables:
            return name
    return next(iter(schema.tables), None)


def _pick_column(table, kind: InvariantKind) -> str | None:
    if kind in {"non_negative", "conservation"}:
        checked = table.non_negative_columns()
        if checked:
            return checked[0]
        for name, column in table.columns.items():
            if column.numeric and name not in table.primary_key and not name.endswith("_id"):
                return name
        return None
    for unique in table.unique:
        useful = [c for c in unique if c not in table.primary_key]
        if useful:
            return useful[0]
    for name in table.columns:
        if name.endswith("_id") or name in {"key", "code", "email", "token", "external_id"}:
            return name
    return None


# --------------------------------------------------------------------------
# Source 3: schema reflection
# --------------------------------------------------------------------------


def propose_from_schema(
    schema: Schema,
    tables: list[str],
    *,
    columns: list[str] | None = None,
    pattern: str | None = None,
) -> Candidate | None:
    table_name = _pick_table(schema, tables)
    if table_name is None:
        return None
    table = schema.table(table_name)
    if table is None:
        return None

    # A CHECK (col >= 0) is the strongest signal there is: the database already
    # states the rule, so the invariant is not inferred at all, it is read.
    checked = table.non_negative_columns()
    if checked:
        column = next((c for c in checked if not columns or c in columns), checked[0])
        sql, params, predicate = check_sql_for("non_negative", table_name, column)
        return Candidate(
            invariant=Invariant(
                kind="non_negative",
                table=table_name,
                column=column,
                predicate=predicate,
                check_sql=sql,
                params=params,
                source="schema",
                derivation=(
                    f"read directly from the CHECK constraint on "
                    f"{table_name}.{column} in the live schema"
                ),
            )
        )

    # A unique index on a natural key states a duplicate_insert / single_use rule.
    for unique in table.unique:
        useful = [c for c in unique if c not in table.primary_key]
        if not useful:
            continue
        kind: InvariantKind = "single_use" if pattern == "single_use" else "duplicate_insert"
        sql, params, predicate = check_sql_for(kind, table_name, None, key_columns=useful)
        return Candidate(
            invariant=Invariant(
                kind=kind,
                table=table_name,
                column=useful[0],
                predicate=predicate,
                check_sql=sql,
                params=params,
                source="schema",
                derivation=(
                    f"read from the unique index on "
                    f"{table_name}({', '.join(useful)}) in the live schema"
                ),
            )
        )

    # A non nullable numeric column the flagged handler decrements. This is a
    # guess, and it says so: `raceguard init` writes it as a commented
    # suggestion rather than adopting it silently.
    for candidate in columns or []:
        column_info = table.columns.get(candidate)
        if column_info is not None and column_info.numeric and not column_info.nullable:
            sql, params, predicate = check_sql_for("non_negative", table_name, candidate)
            return Candidate(
                invariant=Invariant(
                    kind="non_negative",
                    table=table_name,
                    column=candidate,
                    predicate=predicate,
                    check_sql=sql,
                    params=params,
                    source="schema",
                    derivation=(
                        f"inferred, at medium confidence, from {table_name}.{candidate} being "
                        f"a non nullable numeric column that the flagged handler "
                        f"decrements. No constraint states this rule; confirm it in "
                        f"raceguard.yaml."
                    ),
                )
            )
    return None


# --------------------------------------------------------------------------
# Model plumbing
# --------------------------------------------------------------------------


def _call_model(prompt: str, settings: SynthConfig) -> str | None:
    if settings.provider == "command" and settings.command:
        try:
            result = subprocess.run(
                settings.command,
                shell=True,
                input=json.dumps({"prompt": prompt, "version": PROMPT_VERSION}),
                capture_output=True,
                text=True,
                timeout=settings.timeout_s,
            )
        except subprocess.TimeoutExpired:
            log.warning("proposer command timed out after %ss", settings.timeout_s)
            return None
        if result.returncode != 0:
            log.warning("proposer command failed: %s", result.stderr[:400])
            return None
        return result.stdout

    if settings.provider == "claude":
        try:
            import anthropic
        except ImportError:
            log.warning(
                "synth.provider is 'claude' but the anthropic package is not "
                "installed; falling back to schema derivation"
            )
            return None
        try:
            client = anthropic.Anthropic()
            message = client.messages.create(
                model=settings.model or "claude-sonnet-5",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(
                block.text for block in message.content if getattr(block, "type", "") == "text"
            )
        except Exception as exc:  # noqa: BLE001 - a model outage must not break a scan
            log.warning("proposer call failed: %r", exc)
            return None

    if settings.provider == "openai":
        try:
            import openai
        except ImportError:
            return None
        try:
            client = openai.OpenAI()
            response = client.chat.completions.create(
                model=settings.model or "gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content
        except Exception as exc:  # noqa: BLE001
            log.warning("proposer call failed: %r", exc)
            return None
    return None


def _parse_candidate(raw: str, schema: Schema) -> Candidate | None:
    payload = _extract_json(raw)
    if payload is None:
        return None
    try:
        kind = payload["kind"]
        table = payload["table"]
        check_sql = payload["check_sql"]
    except (KeyError, TypeError):
        return None

    # The prompt says only to reference tables in the schema. Trusting that is
    # not the same as checking it, and the check is one line.
    if table not in schema.tables:
        log.warning("proposer named table %r, which is not in the schema; discarded", table)
        return None
    column = payload.get("column")
    if column and not schema.has_column(table, column):
        log.warning("proposer named column %s.%r, which does not exist; discarded", table, column)
        return None

    try:
        invariant = Invariant(
            kind=kind,
            table=table,
            column=column,
            predicate=payload.get("predicate") or f"{table} invariant",
            check_sql=check_sql,
            params=payload.get("params") or {},
            source="spec",
            derivation=payload.get("derivation") or "",
        )
    except Exception:  # noqa: BLE001 - pydantic rejects an unknown kind
        return None
    return Candidate(invariant=invariant, raw=raw)


def _extract_json(raw: str) -> dict | None:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None
