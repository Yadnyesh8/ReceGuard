"""Section 4: the import graph is a rule, not a convention.

* contracts.py imports nothing from the project.
* Engine packages may import contracts, config, and errors, and nothing else
  from each other, with one exception: patch may import harness to reverify.
* Only the adapters (cli.py, action/, ci/, mcp/, init.py, and the mcp_server
  shim) may import more than one engine package.

The reason is not aesthetics. Two adapters that both reimplement orchestration
drift within a month.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "raceguard"
SHARED = {"contracts", "config", "errors"}
ENGINES = {"scanner", "synth", "harness", "hygiene", "patch", "report"}
# `init` turns a scan into a config file: that is orchestration, so it is an
# adapter, not an engine, even though it lives beside them.
# `ci` is the engine every CI system calls and `mcp` is the hosted server:
# both combine scanner, hygiene, harness, patch and report, which is exactly
# what an adapter is for.
ADAPTERS = {"cli", "mcp_server", "mcp", "action", "ci", "init"}
ALLOWED_CROSS = {
    # patch reverifies through the harness (section 4 names this exception)
    ("patch", "harness"),
    # hygiene is explicitly "the same scanner infrastructure, different rules"
    # (section 10): route resolution and the matcher table are shared, not
    # duplicated. Duplicating them is what the rule is guarding against.
    ("hygiene", "scanner"),
}


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE)
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _internal_imports(path: Path) -> set[str]:
    """Top level raceguard packages this file imports, relative or absolute."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    own = _module_name(path).split(".")
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:  # relative
                if node.level == 1:
                    prefix = own[:-1] if len(own) > 1 else []
                else:
                    prefix = own[: max(0, len(own) - node.level)]
                target = (node.module or "").split(".")
                combined = [*prefix, *[p for p in target if p]]
                if combined:
                    found.add(combined[0])
            elif node.module and node.module.startswith("raceguard"):
                parts = node.module.split(".")
                if len(parts) > 1:
                    found.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("raceguard."):
                    found.add(alias.name.split(".")[1])
    return found


def python_files() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def test_contracts_imports_nothing_from_the_project():
    imports = _internal_imports(PACKAGE / "contracts.py")
    assert not imports, f"contracts.py must be a leaf, but imports {sorted(imports)}"


@pytest.mark.parametrize("path", python_files(), ids=lambda p: p.name)
def test_engine_packages_do_not_reach_across(path: Path):
    module = _module_name(path)
    top = module.split(".")[0]
    if top in ADAPTERS or top in SHARED or not module:
        return
    if top not in ENGINES:
        return

    imports = _internal_imports(path)
    foreign = {
        name
        for name in imports
        if name in ENGINES and name != top and (top, name) not in ALLOWED_CROSS
    }
    assert not foreign, (
        f"{module} imports engine package(s) {sorted(foreign)}. Engine packages "
        f"may only share contracts, config, and errors; orchestration belongs in "
        f"an adapter."
    )


def test_adapters_are_the_only_orchestrators():
    """At least one adapter must actually combine engines, or the rule is moot."""
    combined = {
        name: _internal_imports(PACKAGE / f"{name}.py") & ENGINES
        for name in ("cli",)
        if (PACKAGE / f"{name}.py").exists()
    }
    assert any(len(v) >= 2 for v in combined.values()), combined
