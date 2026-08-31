"""Schema reflection: columns, CHECK, UNIQUE, FK.

Source 3 of the three invariant sources. With no config entry and no spec text,
reflect the database and infer:

* a ``CHECK (stock >= 0)`` yields non_negative directly, at full confidence,
* a unique index on a natural key yields duplicate_insert or single_use,
* a non nullable numeric column that the flagged handler decrements yields
  non_negative at medium confidence, which ``raceguard init`` writes into the
  config as a commented suggestion for the user to confirm rather than
  silently adopting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

NUMERIC_TYPES = frozenset(
    {
        "smallint", "integer", "bigint", "decimal", "numeric", "real",
        "double precision", "money", "int2", "int4", "int8", "float4", "float8",
    }
)

_CHECK_GE_ZERO = re.compile(
    r"\(?\s*\(?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)?\s*>=\s*\(?\s*0\s*\)?", re.IGNORECASE
)
_CHECK_GT_ZERO = re.compile(
    r"\(?\s*\(?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)?\s*>\s*\(?\s*0\s*\)?", re.IGNORECASE
)


@dataclass
class Column:
    name: str
    type: str
    nullable: bool
    default: str | None = None

    @property
    def numeric(self) -> bool:
        return self.type.lower() in NUMERIC_TYPES


@dataclass
class Table:
    name: str
    columns: dict[str, Column] = field(default_factory=dict)
    primary_key: list[str] = field(default_factory=list)
    unique: list[tuple[str, ...]] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)  # constraint name -> expression
    foreign_keys: dict[str, tuple[str, str]] = field(default_factory=dict)

    def has(self, column: str) -> bool:
        return column in self.columns

    def non_negative_columns(self) -> list[str]:
        """Columns a CHECK constraint already pins at or above zero."""
        found: list[str] = []
        for expression in self.checks.values():
            for pattern in (_CHECK_GE_ZERO, _CHECK_GT_ZERO):
                match = pattern.search(expression)
                if match and match.group(1) in self.columns:
                    found.append(match.group(1))
        return found


@dataclass
class Schema:
    tables: dict[str, Table] = field(default_factory=dict)

    def table(self, name: str) -> Table | None:
        return self.tables.get(name)

    def has_column(self, table: str, column: str) -> bool:
        found = self.tables.get(table)
        return bool(found and found.has(column))

    def describe(self, names: list[str]) -> str:
        """A compact, model-readable rendering of just the tables in play."""
        lines: list[str] = []
        for name in names:
            table = self.tables.get(name)
            if table is None:
                continue
            lines.append(f"TABLE {name}")
            for info in table.columns.values():
                bits = [f"  {info.name} {info.type}"]
                if not info.nullable:
                    bits.append("NOT NULL")
                if info.name in table.primary_key:
                    bits.append("PRIMARY KEY")
                lines.append(" ".join(bits))
            for unique in table.unique:
                lines.append(f"  UNIQUE ({', '.join(unique)})")
            for constraint, expression in table.checks.items():
                lines.append(f"  CHECK {constraint}: {expression}")
            for column, (ref_table, ref_column) in table.foreign_keys.items():
                lines.append(f"  FOREIGN KEY {column} -> {ref_table}.{ref_column}")
        return "\n".join(lines)


REFLECT_SQL = """
SELECT c.table_name, c.column_name, c.data_type, c.is_nullable, c.column_default
FROM information_schema.columns c
JOIN information_schema.tables t
  ON t.table_name = c.table_name AND t.table_schema = c.table_schema
WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
ORDER BY c.table_name, c.ordinal_position
"""

CONSTRAINT_SQL = """
SELECT tc.table_name, tc.constraint_type, tc.constraint_name, kcu.column_name
FROM information_schema.table_constraints tc
LEFT JOIN information_schema.key_column_usage kcu
  ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
WHERE tc.table_schema = 'public'
ORDER BY tc.table_name, tc.constraint_name, kcu.ordinal_position
"""

CHECK_SQL = """
SELECT tc.table_name, cc.constraint_name, cc.check_clause
FROM information_schema.check_constraints cc
JOIN information_schema.table_constraints tc
  ON tc.constraint_name = cc.constraint_name AND tc.table_schema = cc.constraint_schema
WHERE tc.table_schema = 'public' AND tc.constraint_type = 'CHECK'
"""

FK_SQL = """
SELECT tc.table_name, kcu.column_name, ccu.table_name, ccu.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON kcu.constraint_name = tc.constraint_name
JOIN information_schema.constraint_column_usage ccu
  ON ccu.constraint_name = tc.constraint_name
WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
"""


def reflect(url: str) -> Schema:
    """Read the live schema out of information_schema."""
    from ..db import connect

    schema = Schema()
    with connect(url) as conn:
        for table_name, column_name, data_type, nullable, default in conn.execute(REFLECT_SQL):
            table = schema.tables.setdefault(table_name, Table(name=table_name))
            table.columns[column_name] = Column(
                name=column_name,
                type=str(data_type),
                nullable=(str(nullable).upper() == "YES"),
                default=default,
            )

        grouped: dict[tuple[str, str, str], list[str]] = {}
        for table_name, kind, constraint, column in conn.execute(CONSTRAINT_SQL):
            if column is None:
                continue
            grouped.setdefault((table_name, kind, constraint), []).append(column)
        for (table_name, kind, _constraint), columns in grouped.items():
            found = schema.tables.get(table_name)
            if found is None:
                continue
            if kind == "PRIMARY KEY":
                found.primary_key = columns
                found.unique.append(tuple(columns))
            elif kind == "UNIQUE":
                found.unique.append(tuple(columns))

        for table_name, constraint, clause in conn.execute(CHECK_SQL):
            found = schema.tables.get(table_name)
            # Postgres synthesises a CHECK for every NOT NULL column; those are
            # noise here, not business rules.
            if found is None or str(clause).upper().endswith("IS NOT NULL"):
                continue
            found.checks[constraint] = str(clause)

        for table_name, column_name, ref_table, ref_column in conn.execute(FK_SQL):
            found = schema.tables.get(table_name)
            if found is not None:
                found.foreign_keys[column_name] = (ref_table, ref_column)

        # A UNIQUE INDEX created outside a table constraint does not appear in
        # table_constraints, and it is exactly the guard duplicate_insert cares
        # about, so read pg_index too.
        for table_name, columns in conn.execute(
            """
            SELECT t.relname, array_agg(a.attname ORDER BY k.ord)
            FROM pg_index i
            JOIN pg_class t ON t.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
            WHERE i.indisunique AND n.nspname = 'public'
            GROUP BY t.relname, i.indexrelid
            """
        ):
            found = schema.tables.get(table_name)
            if found is None:
                continue
            key = tuple(columns)
            if key not in found.unique:
                found.unique.append(key)
    return schema
