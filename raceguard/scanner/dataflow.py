"""Pass 3: def-use linking within a handler scope.

Taint propagates through direct assignment, attribute access on a tainted
object, tuple unpacking from a tainted call, and membership in a list or dict
that is later iterated.

No interprocedural analysis in v1. If the write happens inside a helper the
finding is recorded at medium confidence with the call site as the write span.
Interprocedural is the single largest source of both missed bugs and false
positives and it deserves its own release with its own evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import libcst as cst
from libcst.metadata import PositionProvider

from .matchers import (
    GUARD_RETRY_WRAPPER,
    Role,
    Site,
    classify_call,
    column_names,
    dotted,
    string_literal,
)

# Names whose call almost always ends the transaction; used for ordering checks.
_COMMIT_METHODS = frozenset({"commit", "flush"})

# Exception names that mean "PostgreSQL asked you to retry".
_RETRYABLE = frozenset(
    {
        "SerializationFailure",
        "DeadlockDetected",
        "OperationalError",
        "DBAPIError",
        "IntegrityError",
        "TransactionRollbackError",
    }
)
_RETRYABLE_CODES = frozenset({"40001", "40P01", "23505", "23P01"})


@dataclass
class Taint:
    """A local name carrying a value that came out of the database."""

    name: str
    origin: Site
    attributes: set[str] = field(default_factory=set)


@dataclass
class Decision:
    node: cst.CSTNode
    line: int
    end_line: int
    tainted_names: set[str]
    columns: list[str]
    kind: str  # "if" | "assert" | "boolop" | "compare" | "ternary"
    is_threshold: bool  # a comparison against something, not just a None check
    is_existence: bool  # `if x is None` / `if not x` -> create-if-absent shape
    seq: float = 0.0


@dataclass
class Write:
    site: Site | None
    node: cst.CSTNode
    line: int
    end_line: int
    tainted_names: set[str]
    columns: list[str]
    models: list[str]
    kind: str  # "augassign" | "assign_attr" | "call" | "helper_call"
    decrement: bool = False
    guards: set[str] = field(default_factory=set)
    seq: float = 0.0


@dataclass
class HandlerAnalysis:
    reads: list[Site] = field(default_factory=list)
    writes: list[Write] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    taints: dict[str, Taint] = field(default_factory=dict)
    guards: set[str] = field(default_factory=set)
    unresolved: list[str] = field(default_factory=list)
    dynamic_sql: bool = False
    commits: list[int] = field(default_factory=list)
    helper_calls: dict[str, int] = field(default_factory=dict)  # name -> line
    helper_call_nodes: dict[str, cst.Call] = field(default_factory=dict)
    helper_call_seq: dict[str, float] = field(default_factory=dict)
    helper_assign: dict[str, str] = field(default_factory=dict)  # helper -> bound name
    has_retry_wrapper: bool = False
    serializable: bool = False
    statement_lines: dict[int, int] = field(default_factory=dict)
    # True when part of the read-decide-write window was found by following a
    # call into a locally defined helper rather than inside the handler body.
    crossed_boundary: bool = False
    inlined_helpers: list[str] = field(default_factory=list)
    # An insert whose commit sits in a try that catches IntegrityError: the
    # unique constraint is doing the deciding, not the preceding lookup.
    integrity_guarded: bool = False
    # Calls whose SQL could not be resolved statically. Never treated as a
    # read or a write, but never treated as absent either.
    unresolved_sites: list[Site] = field(default_factory=list)


class HandlerVisitor(cst.CSTVisitor):
    """Walks one handler body in source order, building the analysis."""

    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, positions, local_functions: set[str] | None = None) -> None:
        self.positions = positions
        self.local_functions = local_functions or set()
        self.analysis = HandlerAnalysis()
        self._seen_calls: set[int] = set()
        # One Site per Call node. The assignment binder and the call visitor
        # both classify the same node, and taint linking is by identity, so
        # handing out two Site objects for one call silently breaks every link.
        self._sites: dict[int, Site] = {}
        self._node_seq: dict[int, float] = {}
        self._tie_break = 0
        # The handler body is visited more than once (see analyze_handler): a
        # write whose taint only exists after a helper was inlined is invisible
        # on the first pass. These keep the second pass from double counting.
        self._seen_writes: set[tuple[int, str]] = set()
        self._seen_decisions: set[int] = set()

    # -- position helper --------------------------------------------------

    def _span(self, node: cst.CSTNode) -> tuple[int, int]:
        try:
            pos = self.positions[node]
            return pos.start.line, pos.end.line
        except (KeyError, Exception):  # pragma: no cover - metadata gap
            return 0, 0

    # -- reads and writes -------------------------------------------------

    def _seq_for(self, node: cst.CSTNode) -> float:
        """Execution order for one node, stable across re-visits.

        Derived from source position rather than a bare counter: the handler
        body is walked more than once (helper inlining needs a second pass),
        and a counter would hand the same statement a different, larger
        number each time, silently inverting every ordering check in the
        rules. Within the handler body, source position IS execution order.
        Nodes pulled in from an inlined helper are re-stamped afterwards to
        the position of the call that reached them.
        """
        key = id(node)
        existing = self._node_seq.get(key)
        if existing is not None:
            return existing
        start, _ = self._span(node)
        self._tie_break += 1
        seq = float(start) + self._tie_break * 1e-4
        self._node_seq[key] = seq
        return seq

    def _site_for(self, node: cst.Call) -> Site:
        existing = self._sites.get(id(node))
        if existing is not None:
            return existing
        start, end = self._span(node)
        site = classify_call(node, start, end)
        site.seq = self._seq_for(node)
        self._sites[id(node)] = site
        return site

    def visit_Call(self, node: cst.Call) -> None:
        first_visit = id(node) not in self._seen_calls
        self._seen_calls.add(id(node))
        site = self._site_for(node)
        if not first_visit and site.role is not Role.write:
            return
        start, end = site.line, site.end_line

        if site.dynamic_sql:
            self.analysis.dynamic_sql = True
            if site not in self.analysis.unresolved_sites:
                self.analysis.unresolved_sites.append(site)
            self.analysis.unresolved.append(
                f"SQL constructed at runtime at line {start}, not analysable"
            )

        self.analysis.guards |= site.guards

        if site.method in _COMMIT_METHODS:
            self.analysis.commits.append(start)

        if site.role is Role.read:
            # `result.scalar_one_or_none()` names no model of its own; it
            # inherits the one from the statement it is unwrapping.
            if not site.models and site.receiver:
                origin = self.analysis.taints.get(site.receiver)
                if origin is not None:
                    site.models = list(origin.origin.models)
            if site not in self.analysis.reads:
                self.analysis.reads.append(site)
        elif site.role is Role.write:
            self._add_write(
                Write(
                    site=site,
                    node=node,
                    line=start,
                    end_line=end,
                    tainted_names=self._tainted_in(node),
                    columns=site.columns or column_names(node),
                    models=site.models,
                    kind="call",
                    guards=set(site.guards),
                    seq=self._seq_for(node),
                )
            )
        else:
            # A call into a locally defined helper is a possible hidden write.
            base = dotted(node.func)
            if base in self.local_functions:
                self.analysis.helper_calls[base] = start
                self.analysis.helper_call_nodes.setdefault(base, node)
                self.analysis.helper_call_seq.setdefault(base, site.seq)

    # -- assignment and taint --------------------------------------------

    def visit_Assign(self, node: cst.Assign) -> None:
        start, end = self._span(node)
        value = node.value
        origin = self._read_origin(value)

        for target in node.targets:
            self._bind(target.target, origin, value, start, end)

    def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
        if node.value is None:
            return
        start, end = self._span(node)
        self._bind(node.target, self._read_origin(node.value), node.value, start, end)

    def visit_AugAssign(self, node: cst.AugAssign) -> None:
        start, end = self._span(node)
        target = node.target
        tainted = self._tainted_in(target)
        if not tainted:
            return
        decrement = isinstance(node.operator, (cst.SubtractAssign, cst.DivideAssign))
        self._add_write(
            Write(
                site=None,
                node=node,
                line=start,
                end_line=end,
                tainted_names=tainted,
                columns=_attr_names(target),
                models=self._models_of(tainted),
                kind="augassign",
                seq=self._seq_for(node),
                decrement=decrement,
            )
        )

    def _bind(
        self,
        target: cst.BaseExpression,
        origin: Site | None,
        value: cst.BaseExpression,
        start: int,
        end: int,
    ) -> None:
        # Assignment to an attribute of a tainted object is a write.
        if isinstance(target, cst.Attribute):
            tainted = self._tainted_in(target)
            if tainted:
                self._add_write(
                    Write(
                        site=None,
                        node=target,
                        line=start,
                        end_line=end,
                        tainted_names=tainted,
                        columns=_attr_names(target),
                        models=self._models_of(tainted),
                        kind="assign_attr",
                        seq=self._seq_for(target),
                        decrement=_is_decrement(value, target),
                    )
                )
            return

        if origin is None:
            # `item = await _load_item(session, item_id)`: remember which name
            # the helper's return value lands in, so the one-hop pass can bind
            # it to whatever that helper actually read.
            call = value.expression if isinstance(value, cst.Await) else value
            if isinstance(call, cst.Call) and isinstance(target, cst.Name):
                base = dotted(call.func)
                if base in self.local_functions:
                    self.analysis.helper_assign[base] = target.value
            # Propagate taint through ANY expression built from a tainted
            # name: an alias, an attribute read (`s = item.stock`), arithmetic
            # (`new_stock = item.stock - qty`), or membership in a container
            # that is later iterated. Restricting this to aliases and
            # attributes missed the extremely common shape where the new value
            # is computed into a local and then written back.
            if isinstance(target, cst.Name):
                tainted = self._tainted_in(value)
                if tainted:
                    src = self.analysis.taints[next(iter(tainted))]
                    self.analysis.taints[target.value] = Taint(target.value, src.origin)
            return

        if isinstance(target, cst.Name):
            self.analysis.taints[target.value] = Taint(target.value, origin)
        elif isinstance(target, (cst.Tuple, cst.List)):
            for element in target.elements:
                if isinstance(element.value, cst.Name):
                    self.analysis.taints[element.value.value] = Taint(element.value.value, origin)

    def visit_For(self, node: cst.For) -> None:
        """`for row in rows:` keeps the taint on the loop variable."""
        tainted = self._tainted_in(node.iter)
        if not tainted:
            return
        src = self.analysis.taints[next(iter(tainted))]
        if isinstance(node.target, cst.Name):
            self.analysis.taints[node.target.value] = Taint(node.target.value, src.origin)
        elif isinstance(node.target, (cst.Tuple, cst.List)):
            for element in node.target.elements:
                if isinstance(element.value, cst.Name):
                    self.analysis.taints[element.value.value] = Taint(
                        element.value.value, src.origin
                    )

    # -- decisions --------------------------------------------------------

    def visit_If(self, node: cst.If) -> None:
        self._record_decision(node.test, "if")

    def visit_Assert(self, node: cst.Assert) -> None:
        self._record_decision(node.test, "assert")

    def visit_IfExp(self, node: cst.IfExp) -> None:
        self._record_decision(node.test, "ternary")

    def visit_While(self, node: cst.While) -> None:
        self._record_decision(node.test, "if")

    def _record_decision(self, test: cst.BaseExpression, kind: str) -> None:
        tainted = self._tainted_in(test)
        if not tainted:
            return
        if id(test) in self._seen_decisions:
            return
        self._seen_decisions.add(id(test))
        start, end = self._span(test)
        self.analysis.decisions.append(
            Decision(
                node=test,
                line=start,
                end_line=end,
                tainted_names=tainted,
                columns=_attr_names(test),
                kind=kind,
                seq=self._seq_for(test),
                is_threshold=_has_ordering_comparison(test),
                is_existence=_is_existence_test(test),
            )
        )

    # -- retry / isolation detection --------------------------------------

    def visit_Try(self, node: cst.Try) -> None:
        for handler in node.handlers:
            if "IntegrityError" in _exception_names(handler.type):
                statements = list(node.body.body)
                body = cst.Module(body=statements).code.lower()  # type: ignore[arg-type]
                if any(t in body for t in ("commit", "flush", "insert", "add")):
                    self.analysis.integrity_guarded = True
        for handler in node.handlers:
            names = _exception_names(handler.type)
            body_src = _flatten_names(handler.body)
            catches_retryable = (
                bool(names & _RETRYABLE)
                or bool(_RETRYABLE_CODES & _string_constants(handler.body))
                or bool(_RETRYABLE_CODES & _string_constants(handler.type))
            )
            if catches_retryable and ({"retry", "continue", "again", "attempt"} & body_src):
                self.analysis.has_retry_wrapper = True
                self.analysis.guards.add(GUARD_RETRY_WRAPPER)

    # -- helpers ----------------------------------------------------------

    def _read_origin(self, value: cst.BaseExpression) -> Site | None:
        """If this expression is (or wraps) a read call, return its Site."""
        node = value
        if isinstance(node, cst.Await):
            node = node.expression
        if not isinstance(node, cst.Call):
            return None
        site = self._site_for(node)
        if site.role is Role.read and site not in self.analysis.reads:
            # The assignment is visited before its right hand side, so record
            # the read here; visit_Call will reuse this same Site object.
            self.analysis.reads.append(site)
            self.analysis.guards |= site.guards
        return site if site.role is Role.read else None

    def _add_write(self, write: Write) -> None:
        key = (id(write.node), write.kind)
        if key in self._seen_writes:
            return
        self._seen_writes.add(key)
        self.analysis.writes.append(write)

    def _models_of(self, names: set[str]) -> list[str]:
        out: list[str] = []
        for name in names:
            taint = self.analysis.taints.get(name)
            if taint is None:
                continue
            for model in taint.origin.models:
                if model not in out:
                    out.append(model)
        return out

    def _tainted_in(self, node: cst.CSTNode) -> set[str]:
        found: set[str] = set()
        taints = self.analysis.taints

        class _V(cst.CSTVisitor):
            def visit_Name(self, name: cst.Name) -> None:
                if name.value in taints:
                    found.add(name.value)

        node.visit(_V())
        return found


# --------------------------------------------------------------------------
# Free helpers
# --------------------------------------------------------------------------


def _attr_names(node: cst.CSTNode) -> list[str]:
    names: list[str] = []

    class _V(cst.CSTVisitor):
        def visit_Attribute(self, attr: cst.Attribute) -> None:
            if attr.attr.value not in names:
                names.append(attr.attr.value)

    node.visit(_V())
    return names


def _has_ordering_comparison(node: cst.CSTNode) -> bool:
    """`stock < qty` is a threshold; `item is None` is not."""
    found = False

    class _V(cst.CSTVisitor):
        def visit_Comparison(self, cmp: cst.Comparison) -> None:
            nonlocal found
            for target in cmp.comparisons:
                if isinstance(
                    target.operator,
                    (
                        cst.LessThan,
                        cst.LessThanEqual,
                        cst.GreaterThan,
                        cst.GreaterThanEqual,
                    ),
                ):
                    found = True

    node.visit(_V())
    return found


def _is_existence_test(node: cst.CSTNode) -> bool:
    """`if existing:` / `if row is None:` / `if not user:` -> create-if-absent."""
    if isinstance(node, cst.UnaryOperation) and isinstance(node.operator, cst.Not):
        return True
    if isinstance(node, cst.Name):
        return True
    if isinstance(node, cst.Comparison):
        for target in node.comparisons:
            if isinstance(target.operator, (cst.Is, cst.IsNot)):
                if isinstance(target.comparator, cst.Name) and target.comparator.value == "None":
                    return True
    return False


def _is_decrement(value: cst.BaseExpression, target: cst.BaseExpression) -> bool:
    """`item.stock = item.stock - qty` is a decrement written the long way."""
    if not isinstance(value, cst.BinaryOperation):
        return False
    if not isinstance(value.operator, (cst.Subtract, cst.Divide)):
        return False
    names = _attr_names(target)
    return bool(names) and names[0] in _attr_names(value.left)


def _exception_names(node: cst.BaseExpression | None) -> set[str]:
    if node is None:
        return set()
    names: set[str] = set()

    class _V(cst.CSTVisitor):
        def visit_Name(self, name: cst.Name) -> None:
            names.add(name.value)

    node.visit(_V())
    return names


def _flatten_names(node: cst.CSTNode) -> set[str]:
    names: set[str] = set()

    class _V(cst.CSTVisitor):
        def visit_Name(self, name: cst.Name) -> None:
            names.add(name.value.lower())

        def visit_Attribute(self, attr: cst.Attribute) -> None:
            names.add(attr.attr.value.lower())

    node.visit(_V())
    return names


def _string_constants(node: cst.CSTNode | None) -> set[str]:
    if node is None:
        return set()
    out: set[str] = set()

    class _V(cst.CSTVisitor):
        def visit_SimpleString(self, s: cst.SimpleString) -> None:
            value = string_literal(s)
            if value:
                out.add(value)

    node.visit(_V())
    return out


def _bind_parameters(
    visitor: HandlerVisitor, helper: cst.FunctionDef, call: cst.Call | None
) -> None:
    """Map call site arguments onto helper parameter names, positionally and
    by keyword, so taint survives the hop."""
    if call is None:
        return
    params = [p.name.value for p in helper.params.params]
    positional = [a for a in call.args if a.keyword is None]
    for index, arg in enumerate(positional):
        if index >= len(params):
            break
        _transfer(visitor, arg.value, params[index])
    for arg in call.args:
        if arg.keyword is not None and arg.keyword.value in params:
            _transfer(visitor, arg.value, arg.keyword.value)


def _transfer(visitor: HandlerVisitor, source: cst.BaseExpression, param: str) -> None:
    tainted = visitor._tainted_in(source)
    if not tainted:
        return
    origin = visitor.analysis.taints[next(iter(tainted))].origin
    visitor.analysis.taints[param] = Taint(param, origin)


def analyze_handler(
    node: cst.FunctionDef,
    positions,
    local_functions: set[str] | None = None,
    helper_bodies: dict[str, cst.FunctionDef] | None = None,
    max_hops: int = 1,
) -> HandlerAnalysis:
    """Analyse one handler, optionally following one hop into local helpers.

    A single hop is deliberate. FastAPI codebases routinely put the real work
    in a helper (a sync handler bridging to async through ``asyncio.run`` is
    the common case), and refusing to look would miss the bug entirely. Going
    deeper is where interprocedural analysis starts generating both false
    positives and false negatives, so it waits for its own release with its
    own evaluation. Anything found across the hop is reported at medium
    confidence, never high.
    """
    visitor = HandlerVisitor(positions, local_functions)
    node.body.visit(visitor)
    analysis = visitor.analysis

    if not helper_bodies or max_hops < 1:
        return analysis

    before = (len(analysis.reads), len(analysis.writes))
    visited: set[str] = {node.name.value}
    frontier = [name for name in analysis.helper_calls if name in helper_bodies]

    for _ in range(max_hops):
        next_frontier: list[str] = []
        for name in frontier:
            if name in visited:
                continue
            visited.add(name)
            helper = helper_bodies.get(name)
            if helper is None:
                continue
            # Bind the helper's parameters to the taint of the matching
            # argument at the call site. Without this the write inside the
            # helper refers to a parameter name the handler never tainted, and
            # the read and the write never link.
            _bind_parameters(visitor, helper, analysis.helper_call_nodes.get(name))
            reads_before = len(analysis.reads)
            writes_before = len(analysis.writes)
            decisions_before = len(analysis.decisions)
            # Same visitor object: taint, sites and guards stay in one graph,
            # which is what links a read in the helper to a write in the helper
            # and to anything the handler already bound.
            helper.body.visit(visitor)
            # A helper that exists to fetch a row taints the name it was
            # assigned to in the handler.
            # Everything the helper contributed executes AT the call site, not
            # where it happens to sit in the file. Re-stamp it so the ordering
            # checks in the rules see the real execution order.
            call_seq = analysis.helper_call_seq.get(name)
            if call_seq is not None:
                # Rank ACROSS reads, writes and decisions together. Numbering
                # each list from its own zero destroys the relative order among
                # them, which is precisely what the rules test, and the failure
                # is invisible: every item still has a plausible number.
                fresh = (
                    analysis.reads[reads_before:]
                    + analysis.writes[writes_before:]
                    + analysis.decisions[decisions_before:]
                )
                for rank, item in enumerate(sorted(fresh, key=lambda i: i.seq)):
                    item.seq = call_seq + (rank + 1) * 1e-6

            bound = analysis.helper_assign.get(name)
            if bound and len(analysis.reads) > reads_before:
                analysis.taints[bound] = Taint(bound, analysis.reads[reads_before])
            analysis.inlined_helpers.append(name)
            next_frontier.extend(
                n for n in analysis.helper_calls if n in helper_bodies and n not in visited
            )
        frontier = next_frontier

    if analysis.inlined_helpers:
        # A write in the handler that depends on a name only tainted by an
        # inlined helper is invisible on the first pass, because the handler
        # body was walked before the helper existed. One more pass over the
        # handler closes that gap; the dedupe above keeps it idempotent.
        for _ in range(2):
            taints_before = len(analysis.taints)
            writes_before = len(analysis.writes)
            node.body.visit(visitor)
            if len(analysis.taints) == taints_before and len(analysis.writes) == writes_before:
                break

    if (len(analysis.reads), len(analysis.writes)) != before and analysis.inlined_helpers:
        analysis.crossed_boundary = True
    return analysis
