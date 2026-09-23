"""The baseline: findings a team has looked at and chosen not to block on yet.

Adopting a race detector on a codebase with forty existing windows cannot mean
forty red builds. The baseline records fingerprints, not ids: an id carries the
read line and a fingerprint does not, so the file survives ordinary edits
above the handler. See ``Finding.fingerprint``.

Stale entries are reported on every check. A baseline that only ever grows
stops being a list of accepted debt and becomes a list nobody reads.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ..contracts import Finding
from ..errors import ConfigError

VERSION = 1
DEFAULT_PATH = ".raceguard/baseline.json"


@dataclass
class Baseline:
    fingerprints: set[str] = field(default_factory=set)
    generated: str = ""
    path: Path | None = None

    @classmethod
    def load(cls, path: Path | str) -> Baseline:
        path = Path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(
                f"baseline {path} does not exist",
                hint="create it with `raceguard baseline write`",
            ) from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(f"baseline {path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("version") != VERSION:
            raise ConfigError(
                f"baseline {path} has an unsupported shape",
                hint=f"expected {{'version': {VERSION}, 'fingerprints': [...]}}",
            )
        entries = raw.get("fingerprints", [])
        if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
            raise ConfigError(f"baseline {path}: 'fingerprints' must be a list of strings")
        return cls(fingerprints=set(entries), generated=str(raw.get("generated", "")), path=path)

    @classmethod
    def from_findings(cls, findings: Iterable[Finding]) -> Baseline:
        return cls(
            fingerprints={f.fingerprint for f in findings if not f.suppressed},
            generated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    def write(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "version": VERSION,
            "generated": self.generated,
            # Sorted so the diff of a regenerated baseline is readable.
            "fingerprints": sorted(self.fingerprints),
        }
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        self.path = path
        return path

    def contains(self, finding: Finding) -> bool:
        return finding.fingerprint in self.fingerprints

    def stale(self, findings: Iterable[Finding]) -> set[str]:
        """Entries no current finding produces: fixed, deleted, or renamed."""
        current = {f.fingerprint for f in findings}
        return self.fingerprints - current
