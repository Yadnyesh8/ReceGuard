"""optimistic_version: make the write conditional on the row not having moved.

rowcount 0 means somebody else won; the caller retries or reports a conflict.
This is the same mechanism the web exposes as ETag plus If-Match, where a stale
validator yields 412 Precondition Failed and the client must re-fetch before
retrying. Ships with a version column migration.
"""

from __future__ import annotations

from .base import StrategyCommand

DESCRIPTION = "Make the write conditional on a version the caller already read."


class OptimisticVersionCommand(StrategyCommand):
    DESCRIPTION = DESCRIPTION

    def leave_FunctionDef(self, original, updated):
        if original.name.value != self.target.handler or self.changed:
            return super().leave_FunctionDef(original, updated)

        located = self.located
        if located.write is None:
            self.bail("could not locate the write to make conditional")
            return super().leave_FunctionDef(original, updated)

        target = self.target
        if not target.resolved_key:
            self.bail(
                "the primary key expression could not be recovered from the "
                "handler, so the WHERE clause would name something undefined"
            )
            return super().leave_FunctionDef(original, updated)
        self.add_import("sqlalchemy", "update")
        version = target.version_column
        seen = f"seen_{version}"

        replacement = self.parse_statements(
            f"# The caller must send the {version} it read, as a body field or an\n"
            f"# If-Match header. Without that there is nothing to compare against.\n"
            f"result = {self.await_kw}{target.session}.execute(\n"
            f"    update({target.model})\n"
            f"    .where(\n"
            f"        {target.model}.{target.pk} == {target.pk_value},\n"
            f"        {target.model}.{version} == {seen},\n"
            f"    )\n"
            f"    .values({version}={target.model}.{version} + 1)\n"
            f")\n"
            f"if result.rowcount == 0:\n"
            f"    # Somebody else wrote this row after we read it. Use 412 instead\n"
            f"    # of 409 if the caller sent If-Match; it is the same check.\n"
            f"    raise HTTPException(status_code=409, detail=\"stale version\")\n"
        )
        self.notes.append(
            f"add {seen} to the request model and set the other columns you intend "
            f"to write inside the same .values() call"
        )
        updated = self.splice(
            updated, replacement, start=located.write, end=located.write + 1
        )
        return super().leave_FunctionDef(original, updated)
