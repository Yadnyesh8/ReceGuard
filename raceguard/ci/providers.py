"""CI providers: where inputs come from and where outputs go.

A provider knows three things and nothing else: how to tell it is running,
what the diff base is, and how to speak to the host (annotations, a step
summary, output variables). Gating, scanning, and verification never live
here; they are the engine's, and every provider gets the same answers.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ProviderName = Literal["github", "gitlab", "generic"]


@dataclass(frozen=True)
class ProviderContext:
    name: ProviderName
    base_ref: str | None
    is_pull_request: bool
    # Repository coordinates for the sticky comment; None when unknown.
    repo: str | None = None
    pr_number: int | None = None
    api_url: str | None = None
    token: str | None = None
    env: Mapping[str, str] = field(default_factory=dict, repr=False)

    @property
    def changed_only_default(self) -> bool:
        """Gate on the diff for pull requests, on everything for pushes."""
        return self.is_pull_request and self.base_ref is not None


def detect(env: Mapping[str, str] | None = None, *, forced: str | None = None) -> ProviderContext:
    env = dict(os.environ if env is None else env)
    name: ProviderName
    if forced and forced != "auto":
        name = forced  # type: ignore[assignment]
    elif env.get("GITHUB_ACTIONS") == "true":
        name = "github"
    elif env.get("GITLAB_CI") == "true":
        name = "gitlab"
    else:
        name = "generic"

    if name == "github":
        event = env.get("GITHUB_EVENT_NAME", "")
        is_pr = event in {"pull_request", "pull_request_target"}
        base = env.get("GITHUB_BASE_REF") or None
        if base and not base.startswith("refs/") and not base.startswith("origin/"):
            # GITHUB_BASE_REF is a bare branch name; the local checkout only
            # has the remote-tracking ref for it.
            base = f"origin/{base}"
        if not is_pr:
            before = env.get("GITHUB_EVENT_BEFORE")
            base = before if before and set(before) != {"0"} else None
        pr_number = _pr_number_from_ref(env.get("GITHUB_REF", "")) if is_pr else None
        return ProviderContext(
            name="github",
            base_ref=base,
            is_pull_request=is_pr,
            repo=env.get("GITHUB_REPOSITORY"),
            pr_number=pr_number,
            api_url=env.get("GITHUB_API_URL", "https://api.github.com"),
            token=env.get("GITHUB_TOKEN") or env.get("INPUT_TOKEN"),
            env=env,
        )

    if name == "gitlab":
        is_mr = bool(env.get("CI_MERGE_REQUEST_IID"))
        base = env.get("CI_MERGE_REQUEST_DIFF_BASE_SHA") if is_mr else None
        if not base:
            before = env.get("CI_COMMIT_BEFORE_SHA")
            base = before if before and set(before) != {"0"} else None
        iid = env.get("CI_MERGE_REQUEST_IID")
        return ProviderContext(
            name="gitlab",
            base_ref=base,
            is_pull_request=is_mr,
            repo=env.get("CI_PROJECT_ID"),
            pr_number=int(iid) if iid and iid.isdigit() else None,
            api_url=env.get("CI_API_V4_URL", "https://gitlab.com/api/v4"),
            token=env.get("GITLAB_TOKEN") or env.get("CI_JOB_TOKEN"),
            env=env,
        )

    return ProviderContext(name="generic", base_ref=None, is_pull_request=False, env=env)


def _pr_number_from_ref(ref: str) -> int | None:
    # refs/pull/123/merge
    parts = ref.split("/")
    if len(parts) >= 3 and parts[1] == "pull" and parts[2].isdigit():
        return int(parts[2])
    return None


def changed_files(base_ref: str | None, *, cwd: Path | None = None) -> tuple[set[str], str | None]:
    """Files touched since ``base_ref``, and a notice when that could not be known.

    The three-dot form diffs against the merge base, which is what a pull
    request means. It fails on a shallow clone, so the two-dot form is the
    fallback. When both fail the set is empty and the caller must NOT treat
    that as "nothing changed": it degrades to scanning everything and says so.
    """
    if not base_ref:
        return set(), None
    for spec in (f"{base_ref}...HEAD", base_ref):
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", spec],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=cwd,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return set(), f"git diff failed ({exc}); gating on every file instead"
        if result.returncode == 0:
            files = {line.strip() for line in result.stdout.splitlines() if line.strip()}
            return files, None
    return (
        set(),
        f"could not diff against {base_ref!r} (shallow clone? fetch-depth: 0 fixes it); "
        f"gating on every file instead",
    )


# --------------------------------------------------------------------------
# Output channels
# --------------------------------------------------------------------------


def github_annotation(level: str, file: str, line: int, title: str, message: str) -> str:
    """A workflow command GitHub renders inline on the diff, no upload needed."""
    safe = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    safe_title = title.replace("%", "%25").replace(",", "%2C").replace(":", "%3A")
    return f"::{level} file={file},line={line},title={safe_title}::{safe}"


def write_github_outputs(env: Mapping[str, str], values: Mapping[str, object]) -> None:
    path = env.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def write_github_summary(env: Mapping[str, str], markdown: str) -> None:
    path = env.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(markdown + "\n")
