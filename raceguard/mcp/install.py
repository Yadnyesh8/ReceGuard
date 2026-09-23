"""Host configuration for the stdio transport.

Each host keeps its own file and its own shape. The command is the absolute
path of this interpreter's ``raceguard`` executable, so the host does not
depend on the shell PATH of whatever launched it; falling back to
``python -m raceguard.cli`` when the console script is missing.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path

from ..errors import UsageError

HOSTS = ("claude-desktop", "claude-code", "cursor", "vscode")


def launch_command() -> tuple[str, list[str]]:
    executable = Path(sys.executable).parent / ("raceguard.exe" if os.name == "nt" else "raceguard")
    if executable.is_file():
        return str(executable), ["mcp", "serve"]
    return sys.executable, ["-m", "raceguard.cli", "mcp", "serve"]


def config_path(host: str, project_root: Path) -> Path:
    home = Path.home()
    if host == "claude-desktop":
        system = platform.system()
        if system == "Darwin":
            return (
                home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
            )
        if system == "Windows":
            return Path(os.environ.get("APPDATA", home)) / "Claude" / "claude_desktop_config.json"
        return home / ".config" / "Claude" / "claude_desktop_config.json"
    if host == "claude-code":
        return project_root / ".mcp.json"
    if host == "cursor":
        return project_root / ".cursor" / "mcp.json"
    if host == "vscode":
        return project_root / ".vscode" / "mcp.json"
    raise UsageError(f"unknown host {host!r}", hint=f"one of {', '.join(HOSTS)}")


def snippet(host: str, project_root: Path) -> dict:
    if host not in HOSTS:
        raise UsageError(f"unknown host {host!r}", hint=f"one of {', '.join(HOSTS)}")
    command, args = launch_command()
    env = {"RACEGUARD_MCP_ROOT": str(project_root)}
    if host == "vscode":
        return {
            "servers": {
                "raceguard": {"type": "stdio", "command": command, "args": args, "env": env}
            }
        }
    return {"mcpServers": {"raceguard": {"command": command, "args": args, "env": env}}}


def install(
    host: str, *, write: bool = False, project_root: str | None = None
) -> tuple[str, Path | None]:
    if host not in HOSTS:
        raise UsageError(f"unknown host {host!r}", hint=f"one of {', '.join(HOSTS)}")
    root = Path(project_root).resolve() if project_root else Path.cwd().resolve()
    document = snippet(host, root)
    target = config_path(host, root)
    rendered = json.dumps(document, indent=2)
    if not write:
        note = f"\n# add this to {target}"
        if host == "claude-code":
            command, args = launch_command()
            note += f"\n# or: claude mcp add raceguard -- {command} {' '.join(args)}"
        return rendered + note, None

    existing: dict = {}
    if target.is_file():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise UsageError(
                f"{target} is not valid JSON: {exc}", hint="fix it or remove it first"
            ) from exc
    key = "servers" if host == "vscode" else "mcpServers"
    existing.setdefault(key, {}).update(document[key])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return json.dumps(existing, indent=2), target
