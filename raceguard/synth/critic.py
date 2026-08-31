"""The adversarial second pass.

The critic receives the spec, the schema, and the candidate, and is asked ONE
question: name a database state that satisfies this check but violates the
requirement, or one that fails this check but is actually acceptable.

The critic never sees the proposer's reasoning. A critic primed with the
proposer's justification agrees with it; that is the whole reason this is a
separate call with a separate prompt rather than a second turn in the same
conversation. ``strip_reasoning`` enforces the blindness in code rather than
trusting the caller to remember.

Whatever the critic says, the candidate still passes the four step gate in
``validate.py``. A counterexample the critic names is CONSTRUCTED and TESTED,
not believed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import SynthConfig
from ..contracts import Invariant
from .proposer import _call_model, _extract_json
from .schema import Schema

log = logging.getLogger("raceguard.synth.critic")

PROMPT_VERSION = "invariant-critic/v1"

CRITIC_PROMPT = """\
You are reviewing a candidate database check. You did not write it and you have
not been told why it was written that way.

Answer exactly one question: is there a database state that SATISFIES this
check but VIOLATES the requirement, or a state that FAILS this check but is
actually acceptable?

Return one JSON object and no prose:

  {{"verdict": "sound" | "unsound",
    "counterexample": null, or
      {{"description": one sentence,
        "statements": [SQL statements that build the state, in order],
        "expected": "should_violate" | "should_hold"}},
    "note": one sentence}}

If you cannot name a concrete state, the verdict is "sound" and the
counterexample is null. Do not speculate; a vague objection is worse than none.

REQUIREMENT
{spec}

SCHEMA
{schema}

CANDIDATE CHECK (returns rows only when the invariant is violated)
{check_sql}
"""


@dataclass
class Counterexample:
    description: str
    statements: list[str] = field(default_factory=list)
    expected: str = "should_violate"  # or "should_hold"


@dataclass
class CriticVerdict:
    sound: bool
    note: str = ""
    counterexample: Counterexample | None = None
    ran: bool = False

    def summary(self) -> str:
        if not self.ran:
            return "critic did not run (no model configured)"
        if self.sound:
            return f"critic found no counterexample: {self.note}" if self.note else "critic found no counterexample"
        return f"critic rejected the candidate: {self.note}"


def strip_reasoning(invariant: Invariant) -> Invariant:
    """Hand the critic the check, never the argument for it.

    ``derivation`` is the proposer's justification. Passing it through would
    prime the critic into agreement, which is precisely the failure mode this
    pass exists to avoid.
    """
    return invariant.model_copy(update={"derivation": "", "critic_note": None})


def criticise(
    invariant: Invariant,
    spec: str,
    schema: Schema,
    *,
    settings: SynthConfig,
) -> CriticVerdict:
    if not settings.critic_enabled or settings.provider == "none":
        return CriticVerdict(sound=True, ran=False)

    blind = strip_reasoning(invariant)
    prompt = CRITIC_PROMPT.format(
        spec=spec.strip() or "(no requirement sentence was supplied)",
        schema=schema.describe([blind.table]),
        check_sql=blind.check_sql,
    )
    raw = _call_model(prompt, settings)
    if not raw:
        return CriticVerdict(sound=True, ran=False, note="critic call produced no output")

    payload = _extract_json(raw)
    if not isinstance(payload, dict):
        return CriticVerdict(sound=True, ran=False, note="critic output was not JSON")

    verdict = str(payload.get("verdict", "sound")).lower()
    note = str(payload.get("note") or "")
    raw_counter = payload.get("counterexample")
    counterexample = None
    if isinstance(raw_counter, dict) and raw_counter.get("statements"):
        counterexample = Counterexample(
            description=str(raw_counter.get("description") or ""),
            statements=[str(s) for s in raw_counter["statements"]],
            expected=str(raw_counter.get("expected") or "should_violate"),
        )
    return CriticVerdict(
        sound=(verdict != "unsound"), note=note, counterexample=counterexample, ran=True
    )


def test_counterexample(
    invariant: Invariant,
    counterexample: Counterexample,
    *,
    admin_url: str,
    template: str = "rg_template",
    seed: dict[str, list[dict]] | None = None,
) -> tuple[bool, str]:
    """Build the state the critic named and see what the check actually does.

    Returns (candidate_survived, explanation). The critic's claim is never
    taken on trust: if it names a state, that state is constructed and the SQL
    is run against it.
    """
    from .. import db

    name, url = db.create_trial_database(admin_url, 9500, template=template)
    try:
        if seed:
            db.seed(url, seed)
        with db.connect(url) as conn:
            for statement in counterexample.statements:
                conn.execute(statement)
            conn.commit()
        violated, rows = db.check_invariant(url, invariant.check_sql, invariant.params)
    except Exception as exc:  # noqa: BLE001 - an unbuildable state proves nothing
        return True, (
            f"the critic's counterexample could not be built ({exc!r}), so it does "
            f"not disprove the candidate"
        )
    finally:
        db.drop_trial_database(admin_url, name)

    if counterexample.expected == "should_violate":
        if violated:
            return True, (
                f"the critic's counterexample ({counterexample.description}) was built "
                f"and the check DID detect it, so the objection does not stand"
            )
        return False, (
            f"the critic's counterexample was built and the check returned zero rows: "
            f"{counterexample.description}. The check does not detect a state that "
            f"violates the requirement."
        )

    if violated:
        return False, (
            f"the critic's counterexample was built and the check flagged it, but the "
            f"state is acceptable: {counterexample.description}. The check is stricter "
            f"than the requirement and would report a violation that is not one."
        )
    return True, (
        f"the critic's counterexample ({counterexample.description}) was built and the "
        f"check correctly stayed silent, so the objection does not stand"
    )
