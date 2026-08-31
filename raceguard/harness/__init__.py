"""The verification harness: verify(target) -> Verdict.

Trial protocol (section 8.5): create db, seed, boot or reuse app, fire, collect
responses, run check_sql, snapshot state, drop db.

The guard rails matter more than the happy path, because each one is a way the
tool could otherwise report a comfortable lie:

* every request 5xx in a trial -> the trial is an error and leaves the rate
  denominator. An invariant that holds because the app crashed is not a pass.
* no 2xx across all trials -> abort the target with a configuration error. The
  payload or the auth is wrong, and reporting "not reproduced" would be a lie.
* final state equals seed state everywhere -> warn that no write happened,
  which usually means the payload never reached the handler under test.
"""

from __future__ import annotations

import logging
import re
import signal
import time
import uuid
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field

from .. import db
from ..config import Config, Target
from ..contracts import Invariant, Mode, SyncStrategy, TrialRecord, Verdict
from ..errors import BudgetExceeded, InfrastructureError, TargetConfigError
from .appboot import boot_container, boot_inprocess, build_app_image, remove_app_image
from .database import ContainerHandle, start_postgres
from .fire import Request, fire
from .governor import Governor
from .stats import conclusion_footer, summarise

log = logging.getLogger("raceguard.harness")


@dataclass
class RunContext:
    """One container, one template, one governor, shared by every target."""

    config: Config
    run_id: str
    admin_url: str
    template_url: str
    governor: Governor
    container: ContainerHandle
    trial_databases: list[str] = field(default_factory=list)
    app_image: str | None = None

    def ensure_app_image(self) -> str | None:
        """Build the application image once, on first use."""
        if self.app_image is not None:
            return self.app_image
        if not self.config.app.dockerfile:
            return None
        self.app_image = build_app_image(
            self.config.app.dockerfile, self.config.root, self.run_id
        )
        return self.app_image

    def cleanup(self) -> None:
        for name in list(self.trial_databases):
            db.drop_trial_database(self.admin_url, name)
            self.trial_databases.remove(name)
        if self.app_image is not None:
            remove_app_image(self.app_image)
            self.app_image = None
        self.container.stop()


def start_run(config: Config, *, run_id: str | None = None) -> RunContext:
    """Start the container, build the template, and arm the governor."""
    run_id = run_id or uuid.uuid4().hex
    container = start_postgres(
        config.database.image,
        run_id=run_id,
        extensions=tuple(config.database.extensions),
    )

    governor = Governor(
        max_global_concurrency=config.governor.max_global_concurrency,
        min_trial_spacing_ms=config.governor.min_trial_spacing_ms,
        max_requests_per_run=config.governor.max_requests_per_run,
        respect_rate_limit_headers=config.governor.respect_rate_limit_headers,
        refill_per_second=config.governor.refill_per_second,
        burst_capacity=config.governor.burst_capacity,
        external_target=config.governor.allow_external_target,
        external_concurrency_ceiling=config.governor.external_concurrency_ceiling,
    )
    Governor.check_target_ownership(
        container.url, allow_external=config.governor.allow_external_target,
        confirmed_host=_hostname(container.url),
    )

    template_url = db.create_template(
        container.url,
        migrate_command=config.app.migrate,
        cwd=str(config.root),
        database_url_env=config.app.database_url_env,
        env=config.app.env,
        template=config.database.template_name,
        driver=config.app.database_url_driver,
    )
    context = RunContext(
        config=config,
        run_id=run_id,
        admin_url=container.url,
        template_url=template_url,
        governor=governor,
        container=container,
    )
    _install_signal_handlers(context)
    return context


def _hostname(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).hostname or ""


def _install_signal_handlers(context: RunContext) -> None:
    """Containers and trial databases must not outlive an interrupted run."""

    def _handler(signum, frame):  # pragma: no cover - signal path
        log.warning("signal %s received, cleaning up run %s", signum, context.run_id)
        context.cleanup()
        raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(ValueError, OSError):
            signal.signal(sig, _handler)


# --------------------------------------------------------------------------
# One trial
# --------------------------------------------------------------------------


async def run_trial(
    context: RunContext,
    target: Target,
    invariant: Invariant,
    *,
    index: int,
    concurrency: int,
    mode: Mode,
    sync: SyncStrategy,
    amplify_ms: int,
    timeout_s: int,
) -> TrialRecord:
    config = context.config
    record = TrialRecord(index=index, concurrency=concurrency)
    started = time.perf_counter()
    name, trial_url = db.create_trial_database(
        context.admin_url, index, template=config.database.template_name
    )
    context.trial_databases.append(name)

    try:
        db.seed(trial_url, target.seed)
        seed_state = db.snapshot(trial_url, _tables(target, invariant))

        requests = _requests_for(target, concurrency)
        amplify_table = target.amplify_table or invariant.table

        await context.governor.before_trial()
        await context.governor.acquire(concurrency)
        try:
            context.governor.note_request(concurrency)
            result = await _fire_against_app(
                context,
                target,
                trial_url,
                requests,
                mode=mode,
                sync=sync,
                concurrency=concurrency,
                amplify_table=amplify_table,
                amplify_ms=amplify_ms,
                timeout_s=timeout_s,
            )
        finally:
            context.governor.release(concurrency)
            context.governor.after_trial()

        record.status_codes = result.status_codes
        record.arrival_spread_ms = round(result.arrival_spread_ms, 3)

        for headers in result.headers:
            snapshot = context.governor.observe(headers)
            if snapshot.exhausted:
                record.error = "target reported its rate limit as exhausted"

        paired = zip(
            result.status_codes,
            result.headers or [None] * len(result.status_codes),
            strict=False,
        )
        for code, headers in paired:
            if code in (429, 503):
                await context.governor.backpressure(code, headers)

        if result.errors and not result.status_codes:
            record.error = "; ".join(result.errors[:3])

        violated, rows = db.check_invariant(trial_url, invariant.check_sql, invariant.params)
        record.violated = violated
        record.final_state = {
            "tables": db.snapshot(trial_url, _tables(target, invariant)),
            "violating_rows": rows[:10],
        }
        if record.final_state["tables"] == seed_state:
            record.final_state["_unchanged"] = True
    except BudgetExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 - one bad trial must not kill the target
        record.error = repr(exc)
    finally:
        record.duration_s = round(time.perf_counter() - started, 3)
        db.drop_trial_database(context.admin_url, name)
        if name in context.trial_databases:
            context.trial_databases.remove(name)
    return record


async def _fire_against_app(
    context: RunContext,
    target: Target,
    trial_url: str,
    requests: list[Request],
    *,
    mode: Mode,
    sync: SyncStrategy,
    concurrency: int,
    amplify_table: str | None,
    amplify_ms: int,
    timeout_s: int,
):
    config = context.config
    async with AsyncExitStack() as stack:
        if mode == "inprocess":
            if not config.app.module:
                raise TargetConfigError(
                    "inprocess mode needs app.module in raceguard.yaml",
                    hint='app.module looks like "shop.main:app"',
                )
            booted = await stack.enter_async_context(
                boot_inprocess(
                    module_path=config.app.module,
                    database_url=_app_url(config, trial_url),
                    database_url_env=config.app.database_url_env,
                    source_root=config.root,
                    env=config.app.env,
                    concurrency=concurrency,
                    timeout_s=timeout_s,
                )
            )
            if amplify_ms > 0 and booted.engine is not None:
                from .amplify import amplified

                stack.enter_context(amplified(booted.engine, amplify_table, amplify_ms))
        else:
            booted = await stack.enter_async_context(
                boot_container(
                    image=context.ensure_app_image(),
                    dockerfile=config.app.dockerfile,
                    start_command=config.app.start,
                    database_url=_app_url(config, trial_url),
                    database_url_env=config.app.database_url_env,
                    source_root=config.root,
                    health_path=config.app.health_path,
                    run_id=context.run_id,
                    env=config.app.env,
                    concurrency=concurrency,
                    amplify_table=amplify_table,
                    amplify_ms=amplify_ms,
                    timeout_s=timeout_s,
                    port=_port_of(config.app.base_url),
                )
            )

        return await fire(
            sync,
            requests,
            client=booted.client,
            base_url=booted.base_url,
            health_path=config.app.health_path,
            timeout_s=timeout_s,
        )


def _port_of(base_url: str) -> int:
    from urllib.parse import urlparse

    return urlparse(base_url).port or 8000


def _app_url(config: Config, url: str) -> str:
    """The URL in whatever dialect the app under test declared."""
    return db.to_driver_url(url, config.app.database_url_driver)


def _requests_for(target: Target, concurrency: int) -> list[Request]:
    if target.group:
        # Multi endpoint shape: A and B fired together rather than one target
        # repeated. The collision only happens when the two overlap.
        members = target.group
        return [
            Request(
                method=member.route.split(None, 1)[0].upper(),
                path=member.route.split(None, 1)[1],
                payload=member.payload,
                headers=member.headers,
            )
            for member in members
            for _ in range(max(1, concurrency // len(members)))
        ]
    method, path = target.method_and_path()
    return [
        Request(method=method, path=path, payload=target.payload, headers=target.headers)
        for _ in range(concurrency)
    ]


_TABLE_IN_SQL = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?", re.IGNORECASE)


def _tables(target: Target, invariant: Invariant) -> list[str]:
    """Everything worth snapshotting: seeded tables, the invariant's table, and
    any table the check itself reads. A conservation check that joins a second
    table is unreadable in the report without that second table's rows."""
    tables = list(target.seed)
    if invariant.table and invariant.table not in tables:
        tables.append(invariant.table)
    for name in _TABLE_IN_SQL.findall(invariant.check_sql or ""):
        lowered = name.lower()
        if lowered not in {"select", "where", "having", "group", "order"} and lowered not in tables:
            tables.append(lowered)
    return tables


# --------------------------------------------------------------------------
# One target: two phases, reported separately
# --------------------------------------------------------------------------


async def verify_target(
    context: RunContext,
    target: Target,
    invariant: Invariant,
    *,
    finding_id: str = "",
    trials: int | None = None,
    concurrency: int | None = None,
    mode: Mode | None = None,
    amplify: bool | None = None,
) -> Verdict:
    config = context.config
    settings = config.resolved(target)
    trials = trials or settings["trials"]
    concurrency = concurrency or settings["concurrency"]
    mode = mode or settings["mode"]
    sync: SyncStrategy = settings["sync"]
    use_amplify = settings["amplify"] if amplify is None else amplify
    amplify_ms = settings["amplify_ms"] if use_amplify else 0
    if config.governor.allow_external_target:
        # An external target never gets the amplification shim installed.
        amplify_ms = 0

    started = time.perf_counter()
    budget_s = settings["target_budget_s"]
    records: list[TrialRecord] = []

    for index in range(trials):
        if time.perf_counter() - started > budget_s:
            records.append(
                TrialRecord(
                    index=index,
                    concurrency=concurrency,
                    error=f"per target budget of {budget_s}s exhausted",
                )
            )
            break
        records.append(
            await run_trial(
                context,
                target,
                invariant,
                index=index,
                concurrency=concurrency,
                mode=mode,
                sync=sync,
                amplify_ms=amplify_ms,
                timeout_s=settings["trial_timeout_s"],
            )
        )
        # One violating trial is a proof. Keep going only far enough to give
        # the rate meaning, rather than burning the budget after the answer.
        if records[-1].violated and len(records) >= max(5, trials // 4):
            break

    stats = summarise(records)
    notes = list(stats.notes)

    if not any(any(200 <= c < 300 for c in r.status_codes) for r in records):
        # Distinguish "your payload is wrong" from "nothing ever ran". When
        # every trial failed with the same infrastructure error, blaming the
        # payload sends the user to look at exactly the wrong thing, and the
        # real cause is sitting in the trial records.
        errors = [r.error for r in records if r.error]
        if len(errors) == len(records) and errors:
            first = errors[0]
            raise InfrastructureError(
                f"target {target.id!r}: every one of {len(records)} trials failed "
                f"before a request could be answered.\n  {first[:600]}",
                hint=(
                    "this is an infrastructure failure, not a bad payload. Run "
                    "`raceguard doctor`, and check the application image builds "
                    "and starts on its own."
                ),
            )
        raise TargetConfigError(
            f"target {target.id!r}: no request returned 2xx across {len(records)} trials",
            hint=(
                "the payload, the route, or the auth header is wrong. Reporting "
                "'not reproduced' here would be a lie, so the target is aborted "
                "instead."
            ),
        )
    # The blind spot worth naming out loud. An ORM instance decrement
    # (`item.stock -= qty`) makes two concurrent requests both write the SAME
    # value rather than one writing a negative one, so a `stock >= 0` check
    # holds while two units were sold from a stock of one. The invariant is
    # measuring the wrong thing, and reporting "not reproduced" would be a
    # false negative dressed as a pass.
    if not stats.violated and stats.usable:
        crowded = [r for r in records if r.usable and r.successes > 1]
        if crowded:
            worst = max(r.successes for r in crowded)
            notes.append(
                f"WARNING: the invariant held, but {worst} requests succeeded "
                f"together in a single trial against a seed sized for fewer. That "
                f"combination usually means the check is measuring the wrong thing "
                f"rather than that the handler is correct. A read-modify-write done "
                f"through ORM attributes makes both requests write the same value, "
                f"so a non_negative check never trips even though the resource was "
                f"oversold; a conservation invariant comparing the remaining amount "
                f"against the work recorded is what detects that shape."
            )

    if all(r.final_state.get("_unchanged") for r in records if r.final_state):
        notes.append(
            "the final state matched the seed state in every trial, so no write "
            "occurred; the payload probably did not reach the handler under test"
        )
    if amplify_ms == 0:
        notes.append("natural timing: no amplification was applied")
    else:
        notes.append(
            f"amplified: a {amplify_ms}ms delay was inserted after the first read "
            f"of {target.amplify_table or invariant.table!r}, which proves the "
            f"window exists but does not measure how often it fires unaided"
        )
    notes.append(
        conclusion_footer(stats, amplified=amplify_ms > 0, mode=mode, concurrency=concurrency)
    )

    return Verdict(
        finding_id=finding_id or target.finding_id or "",
        target_id=target.id,
        invariant=invariant,
        violated=stats.violated,
        violating_trials=stats.violating,
        total_trials=stats.total,
        usable_trials=stats.usable,
        reproduction_rate=stats.reproduction_rate,
        ci95=stats.ci95,
        amplified=amplify_ms > 0,
        amplify_ms=amplify_ms,
        sync=sync,
        concurrency=concurrency,
        mode=mode,
        duration_s=round(time.perf_counter() - started, 3),
        trials=records,
        notes=notes,
    )


async def verify_two_phase(
    context: RunContext, target: Target, invariant: Invariant, **kwargs
) -> tuple[Verdict, Verdict | None]:
    """Run the amplified phase, then the natural one. Never merge them.

    The amplified verdict is the structural claim: this window exists and is
    exploitable. The natural verdict is the operational claim: this is how
    often it fired with no help. Collapsing them into one number is how an
    honest tool becomes an alarming one.
    """
    amplified_verdict = await verify_target(context, target, invariant, amplify=True, **kwargs)
    if not amplified_verdict.violated:
        return amplified_verdict, None
    natural_verdict = await verify_target(context, target, invariant, amplify=False, **kwargs)
    return amplified_verdict, natural_verdict


# NOTE: there is deliberately no `verify(config, ...)` convenience function
# here. Deriving an invariant and then verifying it is ORCHESTRATION, and
# orchestration lives in the adapters (cli.py, mcp_server.py, action/). Putting
# it here would make the harness import the synthesizer, and two engine
# packages importing each other is exactly the drift the architecture rule in
# section 4 exists to prevent.
