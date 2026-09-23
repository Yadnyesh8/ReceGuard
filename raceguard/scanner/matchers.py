"""Pass 2: site classification.

A matcher table maps call shapes to roles. Kept as data, not code, so adding a
pattern is a table entry rather than a new branch in a growing if-chain.

``execute`` is deliberately ambiguous and is narrowed by inspecting its
argument: a ``select(...)`` makes it a read, ``update(...)``/``insert(...)``
makes it a write, and ``text("...")`` gets a cheap SQL keyword sniff on the
literal. Anything dynamic (``text(build_query())``) drops the finding to low
confidence with the explanation "SQL constructed at runtime, not analysable".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

import libcst as cst
import libcst.matchers as m


class Role(StrEnum):
    read = "read"
    write = "write"
    guard = "guard"
    unknown = "unknown"


# --------------------------------------------------------------------------
# The tables. Order matters only for readability; classification is by lookup.
# --------------------------------------------------------------------------

READ_METHODS = (
    "get",
    "scalar",
    "scalar_one",
    "scalar_one_or_none",
    "scalars",
    "first",
    "one",
    "one_or_none",
    "all",
    "fetchone",
    "fetchall",
    "count",
)

READ_MATCHERS = [m.Call(func=m.Attribute(attr=m.Name(name))) for name in READ_METHODS]

WRITE_METHODS = ("add", "add_all", "delete", "merge", "bulk_save_objects")

WRITE_MATCHERS = [m.Call(func=m.Attribute(attr=m.Name(name))) for name in WRITE_METHODS]

AMBIGUOUS_METHODS = ("execute", "exec_driver_sql", "scalar", "stream")

# SQLAlchemy construct names that decide what an ``execute`` actually is.
_READ_CONSTRUCTS = frozenset({"select", "exists", "union", "union_all"})
_WRITE_CONSTRUCTS = frozenset({"update", "insert", "delete", "upsert"})

_LEADING_KEYWORD_RE = re.compile(r"^\(?\s*(\w+)", re.IGNORECASE)
_SQL_WRITE_RE = re.compile(r"\b(update|insert|delete|merge|truncate)\b", re.IGNORECASE)
_SQL_READ_RE = re.compile(r"\bselect\b", re.IGNORECASE)
_FOR_UPDATE_RE = re.compile(r"\bfor\s+update\b", re.IGNORECASE)
_ADVISORY_RE = re.compile(r"pg_advisory(_xact)?_lock", re.IGNORECASE)
_ON_CONFLICT_RE = re.compile(r"\bon\s+conflict\b", re.IGNORECASE)
_SET_ISOLATION_RE = re.compile(
    r"set\s+transaction\s+isolation\s+level\s+serializable", re.IGNORECASE
)


@dataclass
class Site:
    """One classified call inside a handler body."""

    role: Role
    node: cst.CSTNode
    line: int
    end_line: int
    method: str = ""
    receiver: str = ""  # "session" in session.execute(...)
    models: list[str] = field(default_factory=list)  # ORM class names touched
    columns: list[str] = field(default_factory=list)
    guards: set[str] = field(default_factory=set)  # guard names found on this site
    dynamic_sql: bool = False
    raw_sql: str | None = None
    construct: str | None = None  # "select" | "update" | "insert" | ...
    target_name: str | None = None  # for writes: the name being written
    # Visit order, not file order. A helper defined above the handler holds a
    # write whose LINE precedes the read but whose EXECUTION follows it, so
    # every ordering check in the rules uses seq and never line.
    seq: float = 0.0


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def dotted(node: cst.BaseExpression) -> str:
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr.value}" if base else node.attr.value
    if isinstance(node, cst.Call):
        return dotted(node.func)
    if isinstance(node, cst.Subscript):
        return dotted(node.value)
    return ""


def string_literal(node: cst.BaseExpression) -> str | None:
    if isinstance(node, cst.SimpleString):
        value = node.evaluated_value
        return value if isinstance(value, str) else None
    if isinstance(node, cst.ConcatenatedString):
        left, right = string_literal(node.left), string_literal(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _is_dynamic_string(node: cst.BaseExpression) -> bool:
    """True for f-strings, concatenation with a name, .format(), % formatting."""
    if isinstance(node, cst.FormattedString):
        return True
    if isinstance(node, cst.BinaryOperation):
        return True
    if isinstance(node, cst.Call):
        func = node.func
        if isinstance(func, cst.Attribute) and func.attr.value in {"format", "join"}:
            return True
        return True  # text(build_query())
    if isinstance(node, cst.Name):
        return True
    return False


def _collect_calls(node: cst.CSTNode) -> list[cst.Call]:
    found: list[cst.Call] = []

    class _V(cst.CSTVisitor):
        def visit_Call(self, call: cst.Call) -> None:
            found.append(call)

    node.visit(_V())
    return found


def chain_attrs(node: cst.BaseExpression) -> list[str]:
    """Every name in a call chain, base last: select(X).where().first() ->
    ['first', 'where', 'select'].

    The base name matters: it is what distinguishes ``select(...)`` from
    ``update(...)`` once the chain has been decorated with ``.where()``.
    """
    names: list[str] = []
    current: cst.BaseExpression | None = node
    while current is not None:
        if isinstance(current, cst.Call):
            current = current.func
        elif isinstance(current, cst.Attribute):
            names.append(current.attr.value)
            current = current.value
        elif isinstance(current, cst.Name):
            names.append(current.value)
            break
        else:
            break
    return names


def model_names(node: cst.CSTNode) -> list[str]:
    """ORM class names referenced under a node: select(Item) -> ['Item'].

    A model is heuristically an unqualified Name in CapWords that is not a
    known SQLAlchemy construct. Reflection later maps it to a real table.
    """
    names: list[str] = []

    class _V(cst.CSTVisitor):
        def visit_Name(self, name: cst.Name) -> None:
            value = name.value
            if value and value[0].isupper() and value not in _KNOWN_NON_MODELS:
                if value not in names:
                    names.append(value)

    node.visit(_V())
    return names


_KNOWN_NON_MODELS = frozenset(
    {
        "None",
        "True",
        "False",
        "HTTPException",
        "JSONResponse",
        "Response",
        "Depends",
        "Session",
        "AsyncSession",
        "Decimal",
        "UUID",
        "Any",
        "Optional",
        "List",
        "Dict",
        "IntegrityError",
        "SerializationFailure",
        "OperationalError",
        "DBAPIError",
        "Request",
        "BackgroundTasks",
        "Header",
        "Body",
        "Query",
        "Path",
    }
)


def column_names(node: cst.CSTNode) -> list[str]:
    """Attribute names read off a model: Item.stock -> ['stock']."""
    cols: list[str] = []

    class _V(cst.CSTVisitor):
        def visit_Attribute(self, attr: cst.Attribute) -> None:
            if isinstance(attr.value, cst.Name) and attr.value.value[:1].isupper():
                if attr.attr.value not in cols:
                    cols.append(attr.attr.value)

    node.visit(_V())
    return cols


# --------------------------------------------------------------------------
# Guard recognition, section 6.2 table
# --------------------------------------------------------------------------

GUARD_WITH_FOR_UPDATE = "with_for_update"
GUARD_ATOMIC_EXPRESSION = "atomic_expression"
GUARD_ON_CONFLICT = "on_conflict"
GUARD_RAW_FOR_UPDATE = "raw_for_update"
GUARD_ADVISORY_LOCK = "advisory_lock"
GUARD_UNIQUE_CONSTRAINT = "unique_constraint"
GUARD_SERIALIZABLE = "serializable"
GUARD_RETRY_WRAPPER = "retry_wrapper"
GUARD_VERSION_CHECK = "version_check"

FULL_GUARDS = frozenset(
    {
        GUARD_WITH_FOR_UPDATE,
        GUARD_ATOMIC_EXPRESSION,
        GUARD_ON_CONFLICT,
        GUARD_RAW_FOR_UPDATE,
        GUARD_ADVISORY_LOCK,
        GUARD_UNIQUE_CONSTRAINT,
        GUARD_VERSION_CHECK,
    }
)

_VERSION_COLUMNS = frozenset(
    {"version", "version_id", "updated_at", "etag", "revision", "lock_version"}
)


def guards_on_call(call: cst.Call) -> set[str]:
    """Every guard visible on this call and its argument tree."""
    found: set[str] = set()
    attrs = set(chain_attrs(call))

    if "with_for_update" in attrs:
        found.add(GUARD_WITH_FOR_UPDATE)
    if attrs & {"on_conflict_do_nothing", "on_conflict_do_update"}:
        found.add(GUARD_ON_CONFLICT)

    for inner in _collect_calls(call):
        name = dotted(inner.func)
        tail = name.rsplit(".", 1)[-1]
        if tail == "with_for_update":
            found.add(GUARD_WITH_FOR_UPDATE)
        if tail in {"on_conflict_do_nothing", "on_conflict_do_update"}:
            found.add(GUARD_ON_CONFLICT)
        if tail in {"text", "exec_driver_sql"} and inner.args:
            literal = string_literal(inner.args[0].value)
            if literal:
                if _FOR_UPDATE_RE.search(literal):
                    found.add(GUARD_RAW_FOR_UPDATE)
                if _ADVISORY_RE.search(literal):
                    found.add(GUARD_ADVISORY_LOCK)
                if _ON_CONFLICT_RE.search(literal):
                    found.add(GUARD_ON_CONFLICT)
        if tail in {"pg_advisory_xact_lock", "pg_advisory_lock"}:
            found.add(GUARD_ADVISORY_LOCK)
        if tail == "func" or name.startswith("func."):
            if _ADVISORY_RE.search(name):
                found.add(GUARD_ADVISORY_LOCK)

    # func.pg_advisory_xact_lock(...) shows as an Attribute chain
    chain = dotted(call.func)
    if _ADVISORY_RE.search(chain):
        found.add(GUARD_ADVISORY_LOCK)

    if _atomic_values(call):
        found.add(GUARD_ATOMIC_EXPRESSION)
    if _version_predicate(call):
        found.add(GUARD_VERSION_CHECK)
    return found


def _atomic_values(call: cst.Call) -> bool:
    """``update(Item).values(stock=Item.stock - qty)``: the DB does the arithmetic."""
    for inner in [call, *_collect_calls(call)]:
        func = inner.func
        if not (isinstance(func, cst.Attribute) and func.attr.value == "values"):
            continue
        for arg in inner.args:
            value = arg.value
            if isinstance(value, cst.BinaryOperation):
                # right side must reference a column of a model: Item.stock - qty
                if column_names(value):
                    return True
            if isinstance(value, cst.Call) and dotted(value.func).endswith(
                ("coalesce", "greatest", "least")
            ):
                return True
    return False


def _version_predicate(call: cst.Call) -> bool:
    """``.where(Item.id == id, Item.version == seen)`` is an optimistic guard."""
    for inner in [call, *_collect_calls(call)]:
        func = inner.func
        if not (
            isinstance(func, cst.Attribute) and func.attr.value in {"where", "filter", "filter_by"}
        ):
            continue
        for arg in inner.args:
            if arg.keyword is not None and arg.keyword.value in _VERSION_COLUMNS:
                return True
            for col in column_names(arg.value):
                if col in _VERSION_COLUMNS:
                    return True
    return False


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def classify_call(call: cst.Call, line: int, end_line: int) -> Site:
    func = call.func
    method = func.attr.value if isinstance(func, cst.Attribute) else dotted(func)
    receiver = ""
    if isinstance(func, cst.Attribute):
        receiver = dotted(func.value)

    site = Site(
        role=Role.unknown,
        node=call,
        line=line,
        end_line=end_line,
        method=method,
        receiver=receiver,
    )
    site.guards = guards_on_call(call)

    # Bare construct calls (select/update/insert) reached through a chain such
    # as `session.execute(update(...))` are handled by the argument inspection
    # below; a chain terminating in .first()/.one() is a read.
    chain = chain_attrs(call)

    if method in AMBIGUOUS_METHODS or method in {"stream", "stream_scalars"}:
        _narrow_ambiguous(call, site)
        if site.role is not Role.unknown:
            _annotate(call, site)
            return site

    if method in WRITE_METHODS:
        site.role = Role.write
        site.construct = "orm_" + method
        _annotate(call, site)
        return site

    if method in READ_METHODS or any(name in READ_METHODS for name in chain):
        # A chain like session.query(Item).with_for_update().first() is a read
        # that already carries its guard.
        if any(name in _WRITE_CONSTRUCTS for name in chain):
            site.role = Role.write
        else:
            site.role = Role.read
        site.construct = "select"
        _annotate(call, site)
        return site

    construct = dotted(func).rsplit(".", 1)[-1]
    if construct in _WRITE_CONSTRUCTS:
        site.role = Role.write
        site.construct = construct
        _annotate(call, site)
        return site
    if construct in _READ_CONSTRUCTS:
        site.role = Role.read
        site.construct = construct
        _annotate(call, site)
        return site

    if construct in {"text", "exec_driver_sql"} and call.args:
        _classify_raw(call.args[0].value, site)
        _annotate(call, site)
        return site

    _annotate(call, site)
    return site


def _narrow_ambiguous(call: cst.Call, site: Site) -> None:
    """Inspect the argument of ``execute`` to decide read vs write."""
    if not call.args:
        return
    arg = call.args[0].value

    if isinstance(arg, cst.Call):
        name = dotted(arg.func).rsplit(".", 1)[-1]
        chain = chain_attrs(arg)
        for candidate in [name, *chain]:
            if candidate in _WRITE_CONSTRUCTS:
                site.role = Role.write
                site.construct = candidate
                return
            if candidate in _READ_CONSTRUCTS:
                site.role = Role.read
                site.construct = candidate
                return
        if name in {"text", "exec_driver_sql"} and arg.args:
            _classify_raw(arg.args[0].value, site)
            return

    if isinstance(arg, (cst.SimpleString, cst.FormattedString, cst.ConcatenatedString)):
        _classify_raw(arg, site)
        return

    if isinstance(arg, cst.Name):
        # execute(stmt) where stmt was built earlier; the linker resolves it.
        site.role = Role.unknown
        site.target_name = arg.value


def _classify_raw(node: cst.BaseExpression, site: Site) -> None:
    literal = string_literal(node)
    if literal is None:
        site.dynamic_sql = True
        site.role = Role.unknown
        return
    site.raw_sql = literal
    leading = _LEADING_KEYWORD_RE.match(literal.lstrip())
    keyword = leading.group(1).lower() if leading else ""
    if keyword in {"update", "insert", "delete", "merge", "truncate"}:
        site.role = Role.write
        site.construct = "raw_write"
    elif keyword in {"select", "with", "table", "values"}:
        site.role = Role.read
        site.construct = "raw_read"
    elif _SQL_WRITE_RE.search(literal):
        site.role = Role.write
        site.construct = "raw_write"
    elif _SQL_READ_RE.search(literal):
        site.role = Role.read
        site.construct = "raw_read"
    if _FOR_UPDATE_RE.search(literal):
        site.guards.add(GUARD_RAW_FOR_UPDATE)
    if _ADVISORY_RE.search(literal):
        site.guards.add(GUARD_ADVISORY_LOCK)
    if _ON_CONFLICT_RE.search(literal):
        site.guards.add(GUARD_ON_CONFLICT)


def _annotate(call: cst.Call, site: Site) -> None:
    site.models = model_names(call)
    site.columns = column_names(call)
    # ``update(Item).values(stock=new_stock)`` names the written column as a
    # keyword, not as an attribute, so column_names alone never sees it.
    for inner in [call, *_collect_calls(call)]:
        func = inner.func
        if isinstance(func, cst.Attribute) and func.attr.value in {"values", "filter_by"}:
            for arg in inner.args:
                if arg.keyword is not None and arg.keyword.value not in site.columns:
                    site.columns.append(arg.keyword.value)
    for inner in [call, *_collect_calls(call)]:
        name = dotted(inner.func).rsplit(".", 1)[-1]
        if name in {"text", "exec_driver_sql"} and inner.args:
            if _is_dynamic_string(inner.args[0].value):
                site.dynamic_sql = True
    if isinstance(call.args[0].value, (cst.FormattedString,)) if call.args else False:
        site.dynamic_sql = True


def is_isolation_serializable(node: cst.CSTNode) -> bool:
    """``create_engine(..., isolation_level="SERIALIZABLE")`` or a session variant."""
    for call in _collect_calls(node) or []:
        for arg in call.args:
            if arg.keyword is not None and arg.keyword.value in {
                "isolation_level",
                "execution_options",
            }:
                literal = string_literal(arg.value)
                if literal and literal.upper().replace("_", " ") in {
                    "SERIALIZABLE",
                    "REPEATABLE READ",
                }:
                    return True
            literal = string_literal(arg.value)
            if literal and _SET_ISOLATION_RE.search(literal):
                return True
    return False
