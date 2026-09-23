"""The remediation engine: propose(finding, invariant) -> Patch.

`raceguard fix` prints a diff and exits. `--apply` runs the protocol in
``apply_patch``, which is where the real guarantees live:

1. require a clean git tree (or work on a scratch copy),
2. apply the diff,
3. run the migration against the ephemeral database,
4. reverify the target: expect zero violations in T of T trials, amplified,
5. run the user's test_command,
6. write the change only if BOTH pass; otherwise discard and say which step
   failed, with its output.

Step 5 is not optional. A patch that fixes a race and breaks four tests is
worse than no patch.
"""

from __future__ import annotations

import difflib
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import libcst as cst
from libcst.codemod import CodemodContext

from ..config import Config, Target
from ..contracts import Finding, Invariant, Migration, Patch, Verdict
from ..errors import ConfigError, UsageError
from . import migrations as migration_ops
from .codemods import COMMANDS
from .codemods.base import PatchTarget
from .strategies import Decision, choose

log = logging.getLogger("raceguard.patch")

__all__ = ["propose", "apply_patch", "choose"]

_PK_HINT = re.compile(r"\.get\(\s*\w+\s*,\s*([A-Za-z_][\w.]*)")
_WHERE_PK = re.compile(r"\.(?:id|pk)\s*==\s*([A-Za-z_][\w.]*)")
# `CouponRedemption.coupon_id == coupon.id` -> the right hand side is the value
# the handler already has in scope. Inventing `couponredemption_id` instead
# emits a diff that raises NameError the first time the handler runs.
_WHERE_COLUMN = r"\.{column}\s*==\s*([A-Za-z_][\w.]*)"
_COMPARE = re.compile(r"[<>]=?\s*([A-Za-z_][\w.]*)")
_SESSION = re.compile(r"\b(\w+)\.(?:execute|get|scalar|add|commit)\b")


# --------------------------------------------------------------------------


def build_target(finding: Finding, invariant: Invariant | None) -> PatchTarget:
    """Recover the names the handler already uses, so the diff reads like the file."""
    read = finding.read_site.snippet or ""
    decision = (finding.decision_site.snippet if finding.decision_site else "") or ""
    write = finding.write_site.snippet or ""
    blob = "\n".join([read, decision, write])

    model = _first_model(finding, blob)
    column = (
        finding.columns[0] if finding.columns else (invariant.column if invariant else "amount")
    )
    pk_value = _match(_PK_HINT, read) or _match(_WHERE_PK, read)
    if pk_value is None:
        # Try the columns this finding is actually about before giving up.
        for column_name in finding.columns:
            pk_value = _match(re.compile(_WHERE_COLUMN.format(column=re.escape(column_name))), blob)
            if pk_value:
                break
    resolved_pk = pk_value is not None
    pk_value = pk_value or f"{model.lower()}_id"
    amount = _match(_COMPARE, decision) or "qty"
    session = _match(_SESSION, blob) or "session"
    handler_name = finding.handler.rsplit(":", 1)[-1].rsplit(".", 1)[-1]

    key_columns = list(finding.columns) or (
        [invariant.column] if invariant and invariant.column else []
    )
    group = (finding.columns[0] if finding.columns else None) or "key"
    lock_key = f'"{invariant.table if invariant else model.lower()}:{group}:" + str({pk_value})'

    return PatchTarget(
        handler=handler_name,
        read_line=finding.read_site.start_line,
        write_line=finding.write_site.start_line,
        decision_line=finding.decision_site.start_line if finding.decision_site else None,
        model=model,
        column=column or "amount",
        key_columns=key_columns,
        pk_value=pk_value,
        amount=amount,
        lock_key=lock_key,
        multi_row=(invariant.kind == "conservation") if invariant else False,
        is_async=finding.is_async,
        session=session,
        resolved_key=resolved_pk,
    )


def _first_model(finding: Finding, blob: str) -> str:
    for match in re.finditer(r"\b([A-Z][A-Za-z0-9_]+)\b", blob):
        name = match.group(1)
        if name not in {
            "HTTPException",
            "Depends",
            "AsyncSession",
            "Session",
            "None",
            "True",
            "False",
        }:
            return name
    if finding.tables:
        return "".join(part.capitalize() for part in finding.tables[0].rstrip("s").split("_"))
    return "Model"


def _match(pattern: re.Pattern[str], text: str) -> str | None:
    found = pattern.search(text or "")
    return found.group(1) if found else None


# --------------------------------------------------------------------------


def migration_for(
    decision: Decision, target: PatchTarget, invariant: Invariant | None
) -> Migration | None:
    if not decision.needs_migration:
        return None
    table = invariant.table if invariant else target.model.lower() + "s"
    if decision.strategy == "insert_on_conflict":
        keys = list(target.key_columns) or [target.column]
        return migration_ops.unique_index(table, keys)
    if decision.strategy == "optimistic_version":
        return migration_ops.version_column(table, column=target.version_column)
    return None


def propose(
    finding: Finding,
    invariant: Invariant | None = None,
    *,
    root: Path | str = ".",
    rationale: str | None = None,
) -> Patch:
    """Produce an unapplied unified diff plus a rationale. Never writes to disk."""
    root = Path(root)
    decision = choose(finding, invariant)
    target = build_target(finding, invariant)

    source_path = root / finding.read_site.file
    if not source_path.is_file():
        raise ConfigError(f"cannot patch {finding.read_site.file}: file not found under {root}")
    original = source_path.read_text(encoding="utf-8")

    command_class = COMMANDS[decision.strategy]
    context = CodemodContext(filename=str(source_path))
    command = command_class(context, target)
    try:
        module = cst.parse_module(original)
        transformed = command.transform_module(module)
        new_source = transformed.code
    except Exception as exc:  # noqa: BLE001 - a failed codemod is a no-diff, not a crash
        log.warning("codemod %s failed on %s: %r", decision.strategy, source_path, exc)
        new_source = original
        command.notes.append(f"the transformation could not be applied cleanly: {exc!r}")

    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            new_source.splitlines(keepends=True),
            fromfile=f"a/{finding.read_site.file}",
            tofile=f"b/{finding.read_site.file}",
            n=3,
        )
    )

    notes = list(command.notes)
    if not diff:
        notes.append(
            "no textual change was produced; the handler shape did not match what "
            "this strategy rewrites, so nothing is proposed rather than something "
            "approximate"
        )

    return Patch(
        finding_id=finding.id,
        strategy=decision.strategy,
        diff=diff,
        rationale=rationale or decision.reason,
        migration=migration_for(decision, target, invariant),
        notes=notes,
    )


# --------------------------------------------------------------------------
# The apply protocol
# --------------------------------------------------------------------------


@dataclass
class ApplyResult:
    ok: bool
    failed_step: str | None = None
    output: str = ""
    patch: Patch | None = None


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=120)


def require_clean_tree(root: Path) -> None:
    status = _git(root, "status", "--porcelain")
    if status.returncode != 0:
        raise UsageError(
            f"{root} is not a git repository",
            hint="--apply works on a git tree so the change can be reviewed and reverted",
        )
    # RaceGuard writes its own run artifacts under .raceguard/ during the very
    # command that is about to check this. Counting them as "your uncommitted
    # changes" makes --apply refuse to run because of something the tool did
    # one second earlier.
    dirty = [
        line for line in status.stdout.splitlines() if line.strip() and ".raceguard/" not in line
    ]
    if dirty:
        raise UsageError(
            "the working tree has uncommitted changes:\n  " + "\n  ".join(dirty[:10]),
            hint=(
                "commit or stash first. --apply reverifies and runs your tests, and "
                "it must be able to discard its own change without touching yours."
            ),
        )


def apply_patch(
    patch: Patch,
    config: Config,
    *,
    target: Target | None = None,
    invariant: Invariant | None = None,
    root: Path | str = ".",
    reverify=None,
    run_tests: bool = True,
) -> ApplyResult:
    """Apply, reverify, test, and keep the change only if BOTH pass.

    The guarantee that matters is the negative one: if any step after the diff
    is applied fails, for any reason including an unexpected exception, the
    working tree is put back exactly as it was found. Leaving a user with a
    change they neither accepted nor made is the worst outcome this command
    has, worse than doing nothing at all.
    """
    root = Path(root)
    if not patch.diff.strip():
        return ApplyResult(ok=False, failed_step="propose", output="no diff to apply")

    require_clean_tree(root)

    scratch = Path(tempfile.mkdtemp(prefix="raceguard-apply-"))
    backup = scratch / "backup"
    try:
        # 1. keep a copy, so step 6 can discard cleanly even if git cannot.
        shutil.copytree(
            root,
            backup,
            ignore=shutil.ignore_patterns(
                ".git", ".venv", "node_modules", "__pycache__", ".raceguard"
            ),
        )

        # 2. apply the diff.
        applied = subprocess.run(
            ["git", "apply", "--verbose", "-"],
            cwd=root,
            input=patch.diff,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if applied.returncode != 0:
            return ApplyResult(
                ok=False,
                failed_step="apply",
                output=applied.stderr or applied.stdout,
                patch=patch,
            )

        try:
            result = _verify_and_keep(patch, config, target, invariant, root, reverify, run_tests)
        except Exception as exc:  # noqa: BLE001 - every failure path restores
            _restore(root, backup)
            return ApplyResult(
                ok=False,
                failed_step="reverify",
                output=(f"the change was discarded because verification could not complete: {exc}"),
                patch=patch,
            )
        if not result.ok:
            _restore(root, backup)
        return result
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _verify_and_keep(
    patch: Patch,
    config: Config,
    target: Target | None,
    invariant: Invariant | None,
    root: Path,
    reverify,
    run_tests: bool,
) -> ApplyResult:
    """Steps 3 to 6. Never restores; the caller owns that."""

    # 3. the migration, when the strategy needs one. It is applied to the
    #    reverify template by the reverifier (see cli._make_reverifier) so the
    #    patched code is tested against the schema it references; reverify would
    #    otherwise error on a missing index or column and discard a good fix.
    #    The report still tells the user plainly that the code change alone is
    #    insufficient and the migration must ship with it.
    if patch.migration is not None:
        if reverify is None:
            return ApplyResult(
                ok=False,
                failed_step="migration",
                output=(
                    f"strategy {patch.strategy} requires the schema change below, but "
                    f"there is no target to reverify against, so it cannot be applied "
                    f"and tested safely:\n{patch.migration.statement}"
                ),
                patch=patch,
            )
        log.info(
            "strategy %s requires a schema change; applied to reverify template", patch.strategy
        )

    # 4. reverify: expect zero violations in T of T trials, amplified.
    if reverify is not None and target is not None:
        verdict: Verdict = reverify(target, invariant)
        patch.reverified = verdict
        if verdict.violated:
            return ApplyResult(
                ok=False,
                failed_step="reverify",
                output=(
                    f"after the patch the invariant was STILL violated in "
                    f"{verdict.violating_trials} of {verdict.usable_trials} trials, "
                    f"so the change has been discarded."
                ),
                patch=patch,
            )

    # 5. the user's own test suite. Not optional: a patch that fixes a race and
    #    breaks four tests is worse than no patch.
    if run_tests and config.app.test_command:
        tests = subprocess.run(
            config.app.test_command,
            shell=True,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        patch.test_suite_passed = tests.returncode == 0
        if tests.returncode != 0:
            return ApplyResult(
                ok=False,
                failed_step="test_command",
                output=(
                    f"the patch removed the race but your test suite failed "
                    f"(exit {tests.returncode}), so the change has been "
                    f"discarded.\n\n{tests.stdout[-4000:]}\n{tests.stderr[-4000:]}"
                ),
                patch=patch,
            )

    # 6. both passed: keep it.
    patch.applied = True
    return ApplyResult(ok=True, patch=patch)


def _restore(root: Path, backup: Path) -> None:
    """Put the tree back. git first, then the copy taken in step 1."""
    reverted = subprocess.run(
        ["git", "checkout", "--", "."], cwd=root, capture_output=True, text=True
    )
    if reverted.returncode == 0:
        # checkout reverts tracked files but not files the diff CREATED; those
        # are left untracked. require_clean_tree guaranteed the tree carried no
        # untracked files (bar .raceguard/) before the apply, so anything
        # untracked now was introduced by the patch and must go too, or a
        # discarded patch leaves a stray new file behind. .raceguard/ holds the
        # run's own artifacts and is preserved.
        subprocess.run(
            ["git", "clean", "-fdq", "-e", ".raceguard"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        return
    log.warning("git checkout could not restore %s; falling back to the copy", root)
    for source in backup.rglob("*"):
        if source.is_file():
            destination = root / source.relative_to(backup)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
