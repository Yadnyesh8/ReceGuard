"""The path jail: every path an agent hands us resolves inside one root.

An agent in a loop that decides to "scan the whole disk" is not hypothetical,
and neither is a symlink from inside a checkout to somewhere outside it. The
check is on the RESOLVED path, so symlinks cannot walk out, and the refusal is
a structured error the agent can read rather than an exception it cannot.
"""

from __future__ import annotations

from pathlib import Path

from ..config import CONFIG_FILENAME


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

    def find_config(self, start: Path | None = None) -> Path | None:
        """raceguard.yaml, searched upward from ``start`` but never past the root.

        ``config.find_config`` walks to the filesystem root, which is right for
        a CLI and wrong here. With no config inside the jail it would load one
        from a parent directory, and ``verify_handler`` runs that file's
        ``migrate`` field as a shell command. It is also a plain correctness
        bug: serve ``monorepo/services/billing`` and the monorepo root's config
        is silently used instead, with its targets and its migrate command.
        """
        here = (start or self.root).resolve()
        if here.is_file():
            here = here.parent
        for directory in [here, *here.parents]:
            if self.enforce and not self._inside(directory):
                return None
            candidate = directory / CONFIG_FILENAME
            if candidate.is_file():
                return candidate
        return None

    def anchor(self, path: Path) -> Path:
        """The scan root for a path: config directory, else git root, else the
        sandbox root. Finding ids are relative to it, so it must be stable for
        every tool in a session (see the old project_root note)."""
        start = path if path.is_dir() else path.parent
        config = self.find_config(start)
        if config is not None:
            return config.parent.resolve()
        for directory in [start, *start.parents]:
            if not self._inside(directory):
                break
            if (directory / ".git").exists():
                return directory
        return self.root if self.enforce else start
