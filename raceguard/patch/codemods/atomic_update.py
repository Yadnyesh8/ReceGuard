"""atomic_update: push the decision into the WHERE clause.

    item = await session.get(Item, item_id)
    if item.stock < qty:
        raise HTTPException(409, "out of stock")
    item.stock -= qty

becomes

    result = await session.execute(
        update(Item)
        .where(Item.id == item_id, Item.stock >= qty)
        .values(stock=Item.stock - qty)
    )
    if result.rowcount == 0:
        raise HTTPException(409, "out of stock")

The database evaluates the comparison under its own row lock, so there is no
window between the check and the write for a second request to enter. No extra
lock is held and no round trip is added, which is why this is preferred
whenever the decision fits in SQL.
"""

from __future__ import annotations

from .base import StrategyCommand

DESCRIPTION = "Replace a read-decide-write window with one conditional UPDATE."


class AtomicUpdateCommand(StrategyCommand):
    DESCRIPTION = DESCRIPTION

    def leave_FunctionDef(self, original, updated):
        if original.name.value != self.target.handler or self.changed:
            return super().leave_FunctionDef(original, updated)

        located = self.located
        if located.read is None or located.write is None:
            self.bail("could not locate both the read and the write in this handler")
            return super().leave_FunctionDef(original, updated)

        target = self.target
        if not target.resolved_key:
            self.bail(
                "the primary key expression could not be recovered from the "
                "handler, so the WHERE clause would name something undefined"
            )
            return super().leave_FunctionDef(original, updated)
        self.add_import("sqlalchemy", "update")

        # Keep the original read in place. It is harmless before the atomic
        # UPDATE, and deleting it would leave the handler's response referring
        # to a name that no longer exists.
        start = located.decision if located.decision is not None else located.write
        end = max(located.write, located.decision if located.decision is not None else -1) + 1

        refresh = ""
        name = located.read_target
        if name and self.name_used_after(updated, name, end):
            # Anything read before the UPDATE is now one decrement stale.
            refresh = (
                f"# The row changed inside the UPDATE above, so the copy read\n"
                f"# earlier is stale; refresh it before building the response.\n"
                f"{self.await_kw}{target.session}.refresh({name})\n"
            )
            self.notes.append(
                f"{name} was read before the UPDATE and is stale afterwards; a "
                f"refresh is emitted so the response reports the committed value"
            )

        rejection = self.rejection_or_default(409, "conflict")
        replacement = self.parse_statements(
            f"result = {self.await_kw}{target.session}.execute(\n"
            f"    update({target.model})\n"
            f"    .where(\n"
            f"        {target.model}.{target.pk} == {target.pk_value},\n"
            f"        {target.model}.{target.column} >= {target.amount},\n"
            f"    )\n"
            f"    .values({target.column}={target.model}.{target.column} - {target.amount})\n"
            f")\n"
            f"if result.rowcount == 0:\n"
            f"{rejection}\n" + refresh
        )
        self.notes.append(
            "rowcount 0 now carries the rejection: the comparison happens inside the "
            "UPDATE, so there is no gap between deciding and writing"
        )
        updated = self.splice(updated, replacement, start=start, end=end)
        return super().leave_FunctionDef(original, updated)
