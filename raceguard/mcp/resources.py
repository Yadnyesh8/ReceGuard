"""Resources: reference material a host can read without a tool call.

``raceguard://docs/{name}`` serves the Markdown under ``docs/`` verbatim, so
an agent explaining a verdict can cite the actual wording of the reproduction
rate and guard documents instead of paraphrasing them into a claim the tool
never made.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import find_config, load_config
from ..errors import RaceGuardError
from .runtime import Runtime

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"


def register(mcp, runtime: Runtime) -> None:
    sandbox = runtime.sandbox

    @mcp.resource("raceguard://config", mime_type="application/json")
    def config_resource() -> str:
        """The project's raceguard.yaml, resolved and with secrets redacted."""
        found = find_config(sandbox.root)
        if found is None:
            return json.dumps(
                {"error": f"no raceguard.yaml under {sandbox.root}", "hint": "run `raceguard init`"}
            )
        try:
            return json.dumps(load_config(str(found)).redacted(), indent=2, default=str)
        except RaceGuardError as exc:
            return json.dumps({"error": exc.message, "hint": exc.hint})

    @mcp.resource("raceguard://runs/{run_id}", mime_type="application/json")
    def run_resource(run_id: str) -> str:
        """Artifacts of one recorded CLI run: verdicts, invariants, patches, config."""
        runs = sandbox.root / ".raceguard" / "runs"
        match = next(
            (
                d
                for d in sorted(runs.glob("*"))
                if d.is_dir() and (d.name == run_id or d.name.endswith(run_id))
            ),
            None,
        )
        if match is None:
            return json.dumps({"error": f"no run {run_id!r} under {runs}"})
        out: dict[str, object] = {}
        for file in sorted(match.glob("*.json")):
            try:
                out[file.stem] = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                out[file.stem] = None
        return json.dumps(out, indent=2, default=str)

    @mcp.resource("raceguard://findings/{finding_id}", mime_type="application/json")
    def finding_resource(finding_id: str) -> str:
        """One finding by id, from a fresh scan of the project root."""
        from ..contracts import Confidence
        from ..scanner import ScanOptions
        from ..scanner import scan as run_scan

        result = run_scan(
            [str(sandbox.root)], ScanOptions(min_confidence=Confidence.low, root=sandbox.root)
        )
        finding = next((f for f in result.findings if f.id == finding_id), None)
        if finding is None:
            return json.dumps({"error": f"no finding {finding_id!r}"})
        return finding.model_dump_json(indent=2)

    @mcp.resource("raceguard://docs/{name}", mime_type="text/markdown")
    def docs_resource(name: str) -> str:
        """RaceGuard's own documentation: guards, remediation, reproduction-rate,
        suppression, configuration, security."""
        safe = Path(name).name.removesuffix(".md")
        candidate = DOCS_DIR / f"{safe}.md"
        if not candidate.is_file():
            available = sorted(p.stem for p in DOCS_DIR.glob("*.md")) if DOCS_DIR.is_dir() else []
            return f"no document named {name!r}. Available: {', '.join(available)}"
        return candidate.read_text(encoding="utf-8")
