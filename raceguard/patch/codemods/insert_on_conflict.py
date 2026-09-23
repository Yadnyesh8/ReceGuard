"""insert_on_conflict: let the unique constraint decide.

The constraint is the actual fix; the code change is secondary. This codemod
always ships with a unique index migration, and the report says plainly that
the diff alone is insufficient. ON CONFLICT against a table with no matching
unique index is not a guard: it is an error at best and a silent no-op at
worst.
"""

from __future__ import annotations

from .base import StrategyCommand, _model_kwargs

DESCRIPTION = "Make create-if-absent atomic through a unique constraint."


class InsertOnConflictCommand(StrategyCommand):
    DESCRIPTION = DESCRIPTION

    def leave_FunctionDef(self, original, updated):
        if original.name.value != self.target.handler or self.changed:
            return super().leave_FunctionDef(original, updated)

        located = self.located
        if located.write is None:
            self.bail("could not locate the insert to replace")
            return super().leave_FunctionDef(original, updated)

        target = self.target
        self.add_import("sqlalchemy.dialects.postgresql", "insert")
        keys = [k for k in (list(target.key_columns) or [target.column]) if k]
        elements = ", ".join(repr(k) for k in keys)

        # Reuse the value expressions the handler already wrote. Emitting
        # `email=email` when the handler had `email=payload.email` produces a
        # diff that reads fine and raises NameError.
        # The reported write is usually `session.add(user)`, which carries
        # neither the values nor the bound name. The construction that feeds it
        # is the statement holding both, so look for that first.
        constructed_name, constructed_kwargs = self.find_model_construction(original, target.model)
        kwargs = located.write_kwargs or constructed_kwargs
        if kwargs:
            values = ", ".join(f"{k}={v}" for k, v in kwargs.items())
        else:
            values = ", ".join(f"{k}={k}" for k in keys)
            self.notes.append(
                "the inserted values could not be recovered from the original "
                "statement; check the .values() call in the diff"
            )

        # RETURNING keeps whatever name the handler bound, so the response
        # below the patch still works.
        name = located.write_target or constructed_name or "created"
        replacement = self.parse_statements(
            f"result = {self.await_kw}{target.session}.execute(\n"
            f"    insert({target.model})\n"
            f"    .values({values})\n"
            f"    .on_conflict_do_nothing(index_elements=[{elements}])\n"
            f"    .returning({target.model})\n"
            f")\n"
            f"{name} = result.scalar_one_or_none()\n"
            f"if {name} is None:\n"
            f"    # Another request created this row between the lookup and here.\n"
            f"    # The constraint, not the lookup, is what makes that safe.\n"
            # The handler's own rejection branch, when one was located, so the
            # response shape does not change under the patch.
            + self.rejection_or_default(409, "already exists")
            + "\n"
        )
        self.notes.append(
            f"this change does NOTHING without the unique index on "
            f"{target.model}({', '.join(keys)}); the migration ships with the diff "
            f"and the code change alone is not a fix"
        )
        # Drop the now-pointless lookup and its branch as well, when they were
        # located: leaving them in place is harmless but reads as unfinished.
        start = located.write
        end = located.write + 1
        if located.read is not None and located.read < start:
            start = located.read
        # The construction statement is part of what this replaces; without
        # pulling it into the range it would be left behind, dangling.
        construction_index = self.find_statement_index(
            original, lambda st: bool(_model_kwargs(st, target.model))
        )
        if construction_index is not None:
            start = min(start, construction_index)
            end = max(end, construction_index + 1)
        updated = self.splice(updated, replacement, start=start, end=end)
        return super().leave_FunctionDef(original, updated)
