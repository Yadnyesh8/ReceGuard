"""GitHub Action entrypoint: ``INPUT_*`` in, ``raceguard ci`` semantics out.

Nothing is decided here. The engine in ``raceguard.ci`` scans, gates, verifies
and writes every report; this file maps the action's inputs onto its options
and returns its exit code. The GitLab template and a bare ``raceguard ci`` in
any other system get exactly the same behaviour because they call the same
function.
"""

from __future__ import annotations

import os
import sys

from ..ci import CIOptions, render_text, run


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _optional(name: str) -> str | None:
    raw = os.environ.get(name, "")
    return raw or None


def _tri(name: str) -> bool | None:
    raw = os.environ.get(name, "").strip().lower()
    if raw in {"", "auto"}:
        return None
    return raw in {"1", "true", "yes", "on"}


def main() -> int:
    budget = _optional("INPUT_BUDGET_SECONDS")
    options = CIOptions(
        path=os.environ.get("INPUT_PATH", "."),
        provider="github",
        base=_optional("INPUT_BASE_REF"),
        changed_only=_tri("INPUT_CHANGED_ONLY"),
        fail_on=os.environ.get("INPUT_FAIL_ON", "high"),
        baseline=_optional("INPUT_BASELINE"),
        verify=_flag("INPUT_VERIFY", False),
        verify_required=_flag("INPUT_VERIFY_REQUIRED", False),
        budget_seconds=int(budget) if budget and budget.isdigit() else None,
        hygiene=_flag("INPUT_HYGIENE", True),
        sarif=os.environ.get("INPUT_SARIF_FILE", "raceguard.sarif") or None,
        json_path=_optional("INPUT_JSON_FILE"),
        codequality=_optional("INPUT_CODEQUALITY_FILE"),
        annotations=_flag("INPUT_ANNOTATIONS", True),
        comment=_flag("INPUT_COMMENT", False),
        category=os.environ.get("INPUT_CATEGORY", "raceguard"),
    )
    result = run(options)
    print(render_text(result))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
