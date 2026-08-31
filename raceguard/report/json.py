"""Machine readable output. One shape, versioned, stable across releases."""

from __future__ import annotations

import json as _json
from collections.abc import Iterable

from ..contracts import Patch, RunArtifact, ScanResult, Verdict

FORMAT_VERSION = 1


def build(
    *,
    scan: ScanResult | None = None,
    verdicts: Iterable[Verdict] = (),
    patches: Iterable[Patch] = (),
    tool_version: str = "0.1.0",
    run_id: str = "",
) -> dict:
    return {
        "format_version": FORMAT_VERSION,
        "tool": {"name": "raceguard", "version": tool_version},
        "run_id": run_id,
        "scan": scan.model_dump(mode="json") if scan else None,
        "verdicts": [v.model_dump(mode="json") for v in verdicts],
        "patches": [p.model_dump(mode="json") for p in patches],
        "disclaimer": (
            "A target with no violations was not reproduced under the stated "
            "seed, concurrency, mode and amplification. That is bounded evidence, "
            "not a proof of correctness."
        ),
    }


def render(**kwargs) -> str:
    return _json.dumps(build(**kwargs), indent=2, default=str)


def render_artifact(artifact: RunArtifact) -> str:
    return _json.dumps(artifact.model_dump(mode="json"), indent=2, default=str)
