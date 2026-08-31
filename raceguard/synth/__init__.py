"""The invariant synthesizer: derive(finding, config, schema) -> Invariant.

Three sources tried in order, the winner recorded in ``Invariant.source`` and
the reason in ``Invariant.derivation``. Whatever produced the candidate, it
then passes the same four step mechanical gate. No model output is ever
trusted; it is a proposal that must survive a deterministic check.
"""

from __future__ import annotations

import logging

from ..config import Config, Target
from ..contracts import Finding, Invariant
from ..errors import ConfigError, InvariantRejected
from .critic import criticise, test_counterexample
from .proposer import check_sql_for, propose_from_schema, propose_from_spec
from .schema import Schema, reflect
from .validate import ValidationReport, validate

log = logging.getLogger("raceguard.synth")

__all__ = [
    "derive",
    "derive_for_target",
    "Schema",
    "reflect",
    "validate",
    "ValidationReport",
]


def from_config(target: Target) -> Invariant | None:
    """Source 1. Human authored beats inferred, permanently."""
    spec = target.invariant
    if spec is None:
        return None
    if spec.check_sql:
        return Invariant(
            kind=spec.kind,
            table=spec.table,
            column=spec.column,
            predicate=spec.predicate or f"{spec.table} invariant",
            check_sql=spec.check_sql,
            params=spec.params,
            source="config",
            derivation=f"stated verbatim in raceguard.yaml for target {target.id!r}",
        )
    sql, params, predicate = check_sql_for(
        spec.kind,
        spec.table,
        spec.column,
        key_columns=spec.key_columns or ([spec.column] if spec.column else None),
        expected_total=spec.expected_total,
        limit=spec.limit,
    )
    return Invariant(
        kind=spec.kind,
        table=spec.table,
        column=spec.column,
        predicate=spec.predicate or predicate,
        check_sql=sql,
        params={**params, **spec.params},
        source="config",
        derivation=(
            f"built from the invariant block for target {target.id!r} in "
            f"raceguard.yaml (kind={spec.kind}, table={spec.table}"
            + (f", column={spec.column}" if spec.column else "")
            + ")"
        ),
    )


def derive(
    *,
    config: Config,
    target: Target | None = None,
    finding: Finding | None = None,
    schema: Schema | None = None,
    admin_url: str | None = None,
    template: str = "rg_template",
    strict: bool = True,
) -> Invariant:
    """Produce one validated invariant, or raise with the reason it was rejected."""
    if schema is None:
        if admin_url is None:
            raise ConfigError(
                "invariant derivation needs either a reflected schema or a database URL"
            )
        # Reflect the TEMPLATE, not the admin database. The admin database is
        # where scratch databases get created; the migrated schema lives in the
        # template, and reflecting the wrong one reports every table missing.
        from ..db import with_database

        schema = reflect(with_database(admin_url, template))

    tables = list(target.seed) if target and target.seed else []
    if finding:
        tables = tables or finding.tables
    columns = list(finding.columns) if finding else []
    spec_text = (target.spec if target else None) or (finding.explanation if finding else "")
    seed = target.seed if target else None

    candidates: list[tuple[str, Invariant]] = []

    # -- source 1 -------------------------------------------------------
    if target is not None:
        stated = from_config(target)
        if stated is not None:
            candidates.append(("config", stated))

    # -- source 2 -------------------------------------------------------
    if not candidates and target is not None and target.spec:
        proposed = propose_from_spec(
            target.spec,
            schema,
            tables or list(schema.tables),
            settings=config.synth,
            decision_snippet=(finding.decision_site.snippet if finding and finding.decision_site else ""),
        )
        if proposed is not None:
            candidates.append(("spec", proposed.invariant))

    # -- source 3 -------------------------------------------------------
    if not candidates:
        reflected = propose_from_schema(
            schema,
            tables or list(schema.tables),
            columns=columns,
            pattern=(finding.pattern if finding else None),
        )
        if reflected is not None:
            candidates.append(("schema", reflected.invariant))

    if not candidates:
        raise ConfigError(
            f"could not derive an invariant for "
            f"{target.id if target else (finding.handler if finding else 'this target')}",
            hint=(
                "state one explicitly under `invariant:` in raceguard.yaml, or add a "
                "`spec:` sentence describing the rule in plain words"
            ),
        )

    source, invariant = candidates[0]

    # -- the critic, then the gate. In that order, and the gate always. --
    if source != "config":
        verdict = criticise(invariant, spec_text, schema, settings=config.synth)
        invariant.critic_note = verdict.summary()
        if verdict.counterexample is not None and admin_url:
            survived, explanation = test_counterexample(
                invariant,
                verdict.counterexample,
                admin_url=admin_url,
                template=template,
                seed=seed,
            )
            invariant.critic_note = explanation
            if not survived:
                raise InvariantRejected(
                    f"invariant rejected: {explanation}",
                    step=0,
                    hint=(
                        "the critic named a state, the validator built it, and the "
                        "check behaved wrongly against it. State the invariant "
                        "explicitly in raceguard.yaml."
                    ),
                )

    report = validate(
        invariant,
        schema,
        admin_url=admin_url,
        template=template,
        seed=seed,
        strict=strict and admin_url is not None,
    )
    if not report.passed:
        raise InvariantRejected(
            f"invariant failed the validation gate:\n{report.render()}", step=0
        )
    return invariant


def derive_for_target(
    config: Config,
    target: Target,
    *,
    admin_url: str | None = None,
    template: str = "rg_template",
) -> Invariant:
    return derive(config=config, target=target, admin_url=admin_url, template=template)
