"""Pass 1: handler discovery and route resolution.

Resolves the common indirections: ``@app.post``, ``@router.post``, a router
assigned to a differently named variable, ``APIRouter(prefix=...)`` contributing
to the path, and ``include_router(..., prefix=...)`` in another module
contributing a further prefix.

When a prefix cannot be resolved statically the route is recorded as ``None``
rather than guessed. ``raceguard init`` asks the user rather than inventing one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import libcst as cst
import libcst.matchers as m

HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Objects a route decorator can hang off. ``api_route`` and ``websocket`` are
# Starlette spellings; ``add_api_route`` is the imperative form.
_ROUTER_FACTORIES = frozenset({"APIRouter", "FastAPI", "Starlette", "Router"})


@dataclass
class RouterDef:
    """A router object defined in this module, with whatever prefix we resolved."""

    name: str
    prefix: str = ""
    prefix_resolved: bool = True
    factory: str = "APIRouter"


@dataclass
class RouteInfo:
    method: str
    path: str  # local path as written on the decorator
    router: str | None  # variable name the decorator hung off
    resolved: bool  # False when some prefix could not be determined
    decorator_line: int

    def compose(self, prefix: str) -> str:
        left = prefix.rstrip("/")
        right = self.path if self.path.startswith("/") else "/" + self.path
        return f"{left}{right}" or "/"


@dataclass
class HandlerInfo:
    name: str
    qualname: str
    node: cst.FunctionDef
    is_async: bool
    routes: list[RouteInfo] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    docstring: str | None = None


@dataclass
class IncludeRouter:
    """``app.include_router(orders.router, prefix="/v1")`` seen in some module."""

    target_module: str | None  # "orders" from orders.router, when resolvable
    target_name: str  # "router"
    prefix: str
    resolved: bool


def _string_value(node: cst.BaseExpression) -> str | None:
    """Literal string, or None when it is a name, f-string, or concatenation."""
    if isinstance(node, cst.SimpleString):
        try:
            return node.evaluated_value if isinstance(node.evaluated_value, str) else None
        except Exception:  # pragma: no cover - malformed literal
            return None
    if isinstance(node, cst.ConcatenatedString):
        left = _string_value(node.left)
        right = _string_value(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _kwarg(call: cst.Call, name: str) -> cst.BaseExpression | None:
    for arg in call.args:
        if arg.keyword is not None and arg.keyword.value == name:
            return arg.value
    return None


class RouteCollector(cst.CSTVisitor):
    """Collects routers, route decorators, and include_router calls in one pass."""

    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        self.routers: dict[str, RouterDef] = {}
        self.handlers: list[HandlerInfo] = []
        self.includes: list[IncludeRouter] = []
        self.imports: dict[str, str] = {}  # local name -> dotted source
        self._class_stack: list[str] = []
        self._func_depth = 0

    # -- imports ---------------------------------------------------------

    def visit_Import(self, node: cst.Import) -> None:
        for alias in node.names:
            if not isinstance(alias.name, (cst.Name, cst.Attribute)):
                continue
            dotted = _dotted(alias.name)
            local = dotted.split(".")[0]
            if alias.asname is not None and isinstance(alias.asname.name, cst.Name):
                local = alias.asname.name.value
            self.imports[local] = dotted

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        if isinstance(node.names, cst.ImportStar) or node.module is None:
            return
        module = _dotted(node.module)
        for alias in node.names:
            if not isinstance(alias.name, cst.Name):
                continue
            local = alias.name.value
            if alias.asname is not None and isinstance(alias.asname.name, cst.Name):
                local = alias.asname.name.value
            self.imports[local] = f"{module}.{alias.name.value}"

    # -- router definitions ----------------------------------------------

    def visit_Assign(self, node: cst.Assign) -> None:
        if not isinstance(node.value, cst.Call):
            return
        func = node.value.func
        factory = func.value if isinstance(func, cst.Name) else None
        if factory is None and isinstance(func, cst.Attribute):
            factory = func.attr.value
        if factory not in _ROUTER_FACTORIES:
            return
        prefix_node = _kwarg(node.value, "prefix")
        prefix, resolved = "", True
        if prefix_node is not None:
            value = _string_value(prefix_node)
            if value is None:
                resolved = False
            else:
                prefix = value
        for target in node.targets:
            if isinstance(target.target, cst.Name):
                self.routers[target.target.value] = RouterDef(
                    name=target.target.value,
                    prefix=prefix,
                    prefix_resolved=resolved,
                    factory=factory,
                )

    # -- include_router ---------------------------------------------------

    def visit_Call(self, node: cst.Call) -> None:
        if not m.matches(node, m.Call(func=m.Attribute(attr=m.Name("include_router")))):
            return
        if not node.args:
            return
        target = node.args[0].value
        module_name: str | None = None
        name = ""
        if isinstance(target, cst.Attribute):
            name = target.attr.value
            if isinstance(target.value, cst.Name):
                module_name = target.value.value
        elif isinstance(target, cst.Name):
            name = target.value
        prefix_node = _kwarg(node, "prefix")
        prefix, resolved = "", True
        if prefix_node is not None:
            value = _string_value(prefix_node)
            if value is None:
                resolved = False
            else:
                prefix = value
        self.includes.append(
            IncludeRouter(
                target_module=module_name, target_name=name, prefix=prefix, resolved=resolved
            )
        )

    # -- handlers ---------------------------------------------------------

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self._class_stack.append(node.name.value)

    def leave_ClassDef(self, node: cst.ClassDef) -> None:
        self._class_stack.pop()

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        # Nested functions are not routes; skip descending into them for route
        # purposes but keep walking so include_router inside a factory is seen.
        if self._func_depth > 0:
            self._func_depth += 1
            return True
        self._func_depth += 1

        routes: list[RouteInfo] = []
        names: list[str] = []
        for dec in node.decorators:
            names.append(_decorator_name(dec))
            info = self._route_from_decorator(dec)
            if info is not None:
                routes.append(info)

        qual = ".".join([*self._class_stack, node.name.value])
        if self.module_name:
            qual = f"{self.module_name}:{qual}"
        self.handlers.append(
            HandlerInfo(
                name=node.name.value,
                qualname=qual,
                node=node,
                is_async=node.asynchronous is not None,
                routes=routes,
                decorators=names,
                docstring=node.get_docstring(),
            )
        )
        return True

    def leave_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._func_depth -= 1

    def _route_from_decorator(self, dec: cst.Decorator) -> RouteInfo | None:
        call = dec.decorator
        if not isinstance(call, cst.Call):
            return None
        func = call.func
        if not isinstance(func, cst.Attribute):
            return None
        attr = func.attr.value
        router_name = func.value.value if isinstance(func.value, cst.Name) else None

        if attr in HTTP_METHODS:
            method = attr.upper()
            path_node = call.args[0].value if call.args else None
        elif attr in {"api_route", "route"}:
            path_node = call.args[0].value if call.args else None
            methods_node = _kwarg(call, "methods")
            method = "GET"
            if isinstance(methods_node, (cst.List, cst.Tuple)):
                for element in methods_node.elements:
                    value = _string_value(element.value)
                    if value:
                        method = value.upper()
                        break
        else:
            return None

        if path_node is None:
            return None
        path = _string_value(path_node)
        if path is None:
            return RouteInfo(
                method=method,
                path="",
                router=router_name,
                resolved=False,
                decorator_line=0,
            )
        return RouteInfo(
            method=method, path=path, router=router_name, resolved=True, decorator_line=0
        )


def _dotted(node: cst.BaseExpression) -> str:
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        return f"{_dotted(node.value)}.{node.attr.value}"
    return ""


def _decorator_name(dec: cst.Decorator) -> str:
    node = dec.decorator
    if isinstance(node, cst.Call):
        node = node.func
    return _dotted(node)


def collect_routes(module: cst.Module, module_name: str = "") -> RouteCollector:
    collector = RouteCollector(module_name)
    module.visit(collector)
    return collector


def compose_route(
    handler: HandlerInfo,
    collector: RouteCollector,
    external_prefixes: dict[str, str] | None = None,
    unresolved_routers: set[str] | None = None,
) -> str | None:
    """Compose 'POST /v1/orders' or return None when any prefix is unresolved."""
    if not handler.routes:
        return None
    route = handler.routes[0]
    if not route.resolved:
        return None

    prefix = ""
    if route.router:
        router = collector.routers.get(route.router)
        if router is not None:
            if not router.prefix_resolved:
                return None
            prefix = router.prefix
        if unresolved_routers and route.router in unresolved_routers:
            return None
        if external_prefixes:
            key = (
                f"{collector.module_name}.{route.router}" if collector.module_name else route.router
            )
            outer = external_prefixes.get(key, external_prefixes.get(route.router))
            if outer:
                prefix = outer.rstrip("/") + prefix
    return f"{route.method} {route.compose(prefix)}"
