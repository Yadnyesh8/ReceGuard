"""select_for_update: take the row lock, keep the decision in Python.

Used when the decision involves logic the database cannot evaluate, so it
cannot move into a WHERE clause.

    item = (
        await session.execute(
            select(Item).where(Item.id == item_id).with_for_update()
        )
    ).scalar_one_or_none()

The whole assignment statement is replaced rather than the inner call. Swapping
the call alone leaves the original `await` wrapping the new expression and
produces `await (await ...)`, which is not valid for this shape.

When more than one row is locked, ORDER BY the primary key is NOT optional.
Locking rows in inconsistent order across handlers trades a race for a
deadlock, and a deadlock is the worse bug: it fails under exactly the load the
fix was meant to survive.
"""

from __future__ import annotations

from .base import StrategyCommand

DESCRIPTION = "Take a row lock on the read so the window cannot be entered."


class SelectForUpdateCommand(StrategyCommand):
    DESCRIPTION = DESCRIPTION

    def leave_FunctionDef(self, original, updated):
        if original.name.value != self.target.handler or self.changed:
            return super().leave_FunctionDef(original, updated)

        located = self.located
        if located.read is None:
            self.bail("could not locate the read to lock")
            return super().leave_FunctionDef(original, updated)
        name = located.read_target
        if not name:
            self.bail("the read is not assigned to a name, so it cannot be rewritten safely")
            return super().leave_FunctionDef(original, updated)

        target = self.target
        if not target.resolved_key:
            self.bail(
                "the primary key expression could not be recovered from the "
                "handler, so the WHERE clause would name something undefined"
            )
            return super().leave_FunctionDef(original, updated)
        self.add_import("sqlalchemy", "select")

        order_by = ""
        if target.multi_row:
            order_by = f".order_by({target.model}.{target.pk})"
            self.notes.append(
                f"locked rows are ordered by {target.model}.{target.pk}. Locking in a "
                f"consistent order in every handler is what keeps this from becoming "
                f"a deadlock."
            )

        if target.is_async:
            statement = (
                f"{name} = (\n"
                f"    await {target.session}.execute(\n"
                f"        select({target.model})\n"
                f"        .where({target.model}.{target.pk} == {target.pk_value})\n"
                f"        {order_by}.with_for_update()\n"
                f"    )\n"
                f").scalar_one_or_none()\n"
            )
        else:
            statement = (
                f"{name} = (\n"
                f"    {target.session}.execute(\n"
                f"        select({target.model})\n"
                f"        .where({target.model}.{target.pk} == {target.pk_value})\n"
                f"        {order_by}.with_for_update()\n"
                f"    )\n"
                f").scalar_one_or_none()\n"
            )

        self.notes.append(
            "the decision and the write must stay inside this same transaction; the "
            "lock is released at COMMIT and protects nothing after it"
        )
        updated = self.splice(
            updated, self.parse_statements(statement), start=located.read, end=located.read + 1
        )
        return super().leave_FunctionDef(original, updated)
