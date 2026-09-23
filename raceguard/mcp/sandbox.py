"""The path jail: every path an agent hands us resolves inside one root.

An agent in a loop that decides to "scan the whole disk" is not hypothetical,
and neither is a symlink from inside a checkout to somewhere outside it. The
check is on the RESOLVED path, so symlinks cannot walk out, and the refusal is
a structured error the agent can read rather than an exception it cannot.
"""

from __future__ import annotations

from pathlib import Path

from ..config import find_config


class SandboxViolation(ValueError):
    def __init__(self, requested: str, root: Path) -> None:
        super().__init__(f"{requested!r} resolves outside the project root {root}")
        self.requested = requested
        self.root = root


class Sandbox:
    def __init__(self, root: Path, *, enforce: bool = True) -> None:
        self.root = root.resolve()
        self.enforce = enforce

    def resolve(self, requested: str | Path | None) -> Path:
        """The absolute path for ``requested``, or raise ``SandboxViolation``."""
        raw = Path(requested) if requested not in (None, "", ".") else Path(".")
        candidate = raw if raw.is_absolute() else self.root / raw
        resolved = candidate.resolve()
        if self.enforce and not self._inside(resolved):
            raise SandboxViolation(str(requested), self.root)
        return resolved

    def _inside(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
            return True
        except ValueError:
            return False

    def anchor(self, path: Path) -> Path:
        """The scan root for a path: config directory, else git root, else the
        sandbox root. Finding ids are relative to it, so it must be stable for
        every tool in a session (see the old project_root note)."""
        start = path if path.is_dir() else path.parent
        config = find_config(start)
        if config is not None and self._inside(config.parent.resolve()):
            return config.parent.resolve()
        for directory in [start, *start.parents]:
            if not self._inside(directory):
                break
            if (directory / ".git").exists():
                return directory
        return self.root if self.enforce else start
