"""Shared machinery for the strategy codemods.

The important detail here is how statements are located. Position metadata
resolves against the ORIGINAL tree only, so a codemod that asks for the line of
a node it has already rebuilt gets nothing back and silently emits an import
and no change. Indices are therefore computed once in ``visit_FunctionDef``,
where the node is still the original, and reused in ``leave_FunctionDef``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import libcst as cst
import libcst.matchers as m
from libcst.codemod import CodemodContext, VisitorBasedCodemodCommand
from libcst.codemod.visitors import AddImportsVisitor
from libcst.metadata import PositionProvider


@dataclass
class PatchTarget:
    """Everything a codemod needs to find its statements again."""

    handler: str
    read_line: int
    write_line: int
    decision_line: int | None = None
    model: str = "Item"
    column: str = "stock"
    key_columns: Sequence[str] = ()
    pk: str = "id"
    pk_value: str = "item_id"       # the expression the handler already uses
    amount: str = "qty"             # the quantity subtracted or compared
    version_column: str = "version"
    lock_key: str = '"raceguard"'   # a SQL-quotable expression
    multi_row: bool = False
    is_async: bool = True
    session: str = "session"
    # False when the value the lock or the WHERE clause needs could not be
    # recovered from the handler. A codemod must then emit nothing: a diff
    # naming an invented local is worse than no diff at all.
    resolved_key: bool = True


@dataclass
class Located:
    """Statement indices inside one handler body, resolved on the original tree."""

    read: int | None = None
    decision: int | None = None
    write: int | None = None
    read_target: str | None = None   # the name the read was assigned to
    write_target: str | None = None  # the name the constructed row was bound to
    write_kwargs: dict[str, str] = field(default_factory=dict)
    rejection: list[str] = field(default_factory=list)
    indent: str = "    "


class StrategyCommand(VisitorBasedCodemodCommand):
    """Base for every strategy transformer.

    Scoped to the one handler named in the target. A codemod that rewrites
    every handler in a file because two of them look alike is worse than no
    codemod.
    """

    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, context: CodemodContext, target: PatchTarget) -> None:
        super().__init__(context)
        self.target = target
        self.changed = False
        self._in_handler = False
        self.located = Located()
        self.notes: list[str] = []

    # -- scoping and location -------------------------------------------

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        if node.name.value != self.target.handler:
            return True
        self._in_handler = True
        self.located = self._locate(node)
        return True

    def leave_FunctionDef(
        self, original: cst.FunctionDef, updated: cst.FunctionDef
    ) -> cst.FunctionDef:
        if original.name.value == self.target.handler:
            self._in_handler = False
        return updated

    def _locate(self, node: cst.FunctionDef) -> Located:
        located = Located()
        body = list(node.body.body)
        for index, statement in enumerate(body):
            start, end = self.span(statement)
            if start < 0:
                continue
            if located.read is None and start <= self.target.read_line <= end:
                located.read = index
                located.read_target = _assigned_name(statement)
            if (
                self.target.decision_line is not None
                and located.decision is None
                and start <= self.target.decision_line <= end
            ):
                located.decision = index
                located.rejection = _branch_body(statement)
            if start <= self.target.write_line <= end:
                located.write = index
                located.write_target = _assigned_name(statement)
                located.write_kwargs = _model_kwargs(statement, self.target.model)
        return located

    def span(self, node: cst.CSTNode) -> tuple[int, int]:
        try:
            position = self.get_metadata(PositionProvider, node)
            return position.start.line, position.end.line
        except Exception:  # pragma: no cover - metadata gap
            return -1, -1

    def line_of(self, node: cst.CSTNode) -> int:
        return self.span(node)[0]

    # -- emission helpers ------------------------------------------------

    def add_import(self, module: str, name: str) -> None:
        AddImportsVisitor.add_needed_import(self.context, module, name)

    @property
    def await_kw(self) -> str:
        return "await " if self.target.is_async else ""

    def parse_statements(self, source: str) -> list[cst.BaseStatement]:
        """Build replacements from source, so they stay readable in this file."""
        return list(cst.parse_module(source).body)

    def rejection_or_default(self, status: int = 409, detail: str = "conflict") -> str:
        """Reuse the handler's own rejection so behaviour does not change."""
        if self.located.rejection:
            return "\n".join("    " + line for line in self.located.rejection)
        self.notes.append(
            "the original rejection branch could not be reused verbatim, so a "
            f"{status} is emitted; review it against the behaviour you want"
        )
        return f'    raise HTTPException(status_code={status}, detail="{detail}")'

    def splice(
        self,
        updated: cst.FunctionDef,
        replacement: list[cst.BaseStatement],
        *,
        start: int,
        end: int,
        allow_dangling: bool = False,
    ) -> cst.FunctionDef:
        """Swap body[start:end] for the replacement, leaving everything else byte
        identical. A diff touching unrelated lines will not be merged.

        Refuses the swap if a name bound by the removed statements is still
        referenced afterwards. Emitting a patch that raises NameError the first
        time the handler runs is worse than emitting nothing, and this is the
        one check that catches it every time.
        """
        body = list(updated.body.body)
        removed = body[start:end]
        if removed and not allow_dangling:
            bound = _bound_names(removed)
            introduced = _bound_names(replacement)
            still_used = _referenced_names(body[end:]) & (bound - introduced)
            if still_used:
                names = ", ".join(sorted(still_used))
                self.bail(
                    f"removing the read-decide-write statements would leave "
                    f"{names} undefined further down the handler"
                )
                return updated
        new_body = body[:start] + replacement + body[end:]
        self.changed = True
        return updated.with_changes(body=updated.body.with_changes(body=new_body))

    def find_model_construction(
        self, node: cst.FunctionDef, model: str
    ) -> tuple[str | None, dict[str, str]]:
        """Locate `x = Model(a=..., b=...)` anywhere in the handler.

        The write site the scanner reports is usually `session.add(user)`,
        which carries neither the values nor the name. The construction that
        feeds it is the statement that actually holds both.
        """
        for statement in node.body.body:
            kwargs = _model_kwargs(statement, model)
            if kwargs:
                return _assigned_name(statement), kwargs
        return None, {}

    def find_statement_index(self, node: cst.FunctionDef, predicate) -> int | None:
        for index, statement in enumerate(node.body.body):
            if predicate(statement):
                return index
        return None

    def name_used_after(self, updated: cst.FunctionDef, name: str, index: int) -> bool:
        body = list(updated.body.body)
        return name in _referenced_names(body[index:])

    def bail(self, reason: str) -> None:
        self.notes.append(
            f"{reason}; nothing is proposed rather than something approximate"
        )


def _model_kwargs(statement: cst.CSTNode, model: str) -> dict[str, str]:
    """`user = User(email=payload.email)` -> {'email': 'payload.email'}.

    The handler already wrote the value expressions; reusing them verbatim is
    the difference between a diff that compiles and one that references a bare
    column name that was never a local variable.
    """
    found: dict[str, str] = {}
    renderer = cst.Module(body=[])

    class _V(cst.CSTVisitor):
        def visit_Call(self, call: cst.Call) -> None:
            name = call.func.value if isinstance(call.func, cst.Name) else None
            if name != model:
                return
            for arg in call.args:
                if arg.keyword is not None:
                    found[arg.keyword.value] = renderer.code_for_node(arg.value).strip()

    statement.visit(_V())
    return found


def _bound_names(statements: Sequence[cst.CSTNode]) -> set[str]:
    names: set[str] = set()
    for statement in statements:
        class _V(cst.CSTVisitor):
            def visit_AssignTarget(self, node: cst.AssignTarget) -> None:
                if isinstance(node.target, cst.Name):
                    names.add(node.target.value)

            def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
                if isinstance(node.target, cst.Name):
                    names.add(node.target.value)

        statement.visit(_V())
    return names


def _referenced_names(statements: Sequence[cst.CSTNode]) -> set[str]:
    names: set[str] = set()
    for statement in statements:
        class _V(cst.CSTVisitor):
            def visit_Name(self, node: cst.Name) -> None:
                names.add(node.value)

        statement.visit(_V())
    return names


def _assigned_name(statement: cst.CSTNode) -> str | None:
    if not isinstance(statement, cst.SimpleStatementLine):
        return None
    for small in statement.body:
        if isinstance(small, cst.Assign) and small.targets:
            target = small.targets[0].target
            if isinstance(target, cst.Name):
                return target.value
        if isinstance(small, cst.AnnAssign) and isinstance(small.target, cst.Name):
            return small.target.value
    return None


def _branch_body(statement: cst.CSTNode) -> list[str]:
    """The lines inside `if ...:` so the emitted guard can reuse them."""
    if not isinstance(statement, cst.If):
        return []
    rendered = cst.Module(body=[]).code_for_node(statement.body)
    return [line.strip() for line in rendered.splitlines() if line.strip()]


def is_call_to(node: cst.CSTNode, *names: str) -> bool:
    return m.matches(
        node, m.Call(func=m.Attribute(attr=m.Name(value=m.MatchIfTrue(lambda v: v in names))))
    )
