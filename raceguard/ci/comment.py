"""One sticky comment per pull request, updated in place.

A bot that posts a new comment on every push trains reviewers to collapse it.
The comment carries a hidden marker; when a comment with the marker already
exists it is edited, otherwise one is created. No token means no comment and
a notice, never a failed build: the report already went to SARIF and the step
summary, and a missing permission is not a defect in the code under review.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .providers import ProviderContext

log = logging.getLogger("raceguard.ci.comment")

MARKER = "<!-- raceguard:sticky -->"


@dataclass
class CommentResult:
    posted: bool
    action: str  # "created" | "updated" | "skipped"
    reason: str = ""


def post_sticky(
    context: ProviderContext,
    body: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout_s: float = 20.0,
) -> CommentResult:
    if context.pr_number is None or not context.repo:
        return CommentResult(False, "skipped", "not a pull request")
    if not context.token:
        return CommentResult(
            False,
            "skipped",
            "no token: set GITHUB_TOKEN (pull-requests: write) or GITLAB_TOKEN",
        )
    text = f"{MARKER}\n{body}"
    with httpx.Client(transport=transport, timeout=timeout_s) as client:
        try:
            if context.name == "github":
                return _github(client, context, text)
            if context.name == "gitlab":
                return _gitlab(client, context, text)
        except httpx.HTTPError as exc:
            log.warning("could not post the sticky comment: %r", exc)
            return CommentResult(False, "skipped", f"request failed: {exc}")
    return CommentResult(False, "skipped", f"no comment support for {context.name}")


def _github(client: httpx.Client, context: ProviderContext, text: str) -> CommentResult:
    base = f"{context.api_url}/repos/{context.repo}/issues/{context.pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {context.token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    existing = None
    page = 1
    while page <= 10:
        response = client.get(base, headers=headers, params={"per_page": 100, "page": page})
        response.raise_for_status()
        comments = response.json()
        existing = next((c for c in comments if MARKER in (c.get("body") or "")), None)
        if existing or len(comments) < 100:
            break
        page += 1
    if existing:
        url = f"{context.api_url}/repos/{context.repo}/issues/comments/{existing['id']}"
        client.patch(url, headers=headers, json={"body": text}).raise_for_status()
        return CommentResult(True, "updated")
    client.post(base, headers=headers, json={"body": text}).raise_for_status()
    return CommentResult(True, "created")


def _gitlab(client: httpx.Client, context: ProviderContext, text: str) -> CommentResult:
    base = f"{context.api_url}/projects/{context.repo}/merge_requests/{context.pr_number}/notes"
    # CI_JOB_TOKEN authenticates with a different header than a personal or
    # project token; GITLAB_TOKEN wins when both are set (see providers).
    job_token = context.env.get("CI_JOB_TOKEN")
    headers = (
        {"JOB-TOKEN": context.token}
        if job_token and context.token == job_token
        else {"PRIVATE-TOKEN": context.token or ""}
    )
    existing = None
    page = 1
    while page <= 10:
        response = client.get(base, headers=headers, params={"per_page": 100, "page": page})
        response.raise_for_status()
        notes = response.json()
        existing = next((n for n in notes if MARKER in (n.get("body") or "")), None)
        if existing or len(notes) < 100:
            break
        page += 1
    if existing:
        client.put(
            f"{base}/{existing['id']}", headers=headers, json={"body": text}
        ).raise_for_status()
        return CommentResult(True, "updated")
    client.post(base, headers=headers, json={"body": text}).raise_for_status()
    return CommentResult(True, "created")
