"""Invariant synthesis and, above all, the validation gate (section 7.4)."""

from __future__ import annotations

import pytest

from raceguard.config import SynthConfig, Target
from raceguard.contracts import Invariant
from raceguard.errors import InvariantRejected
from raceguard.synth import from_config
from raceguard.synth.critic import criticise, strip_reasoning
from raceguard.synth.proposer import check_sql_for, propose_from_schema, propose_from_spec
from raceguard.synth.schema import Column, Schema, Table
from raceguard.synth.validate import build_corruption, validate


def items_schema(*, check: bool = False, unique: bool = False) -> Schema:
    table = Table(
        name="items",
        columns={
            "id": Column("id", "integer", False, "nextval('items_id_seq')"),
            "sku": Column("sku", "character varying", False),
            "stock": Column("stock", "integer", False),
        },
        primary_key=["id"],
    )
    if check:
        table.checks["ck_items_stock"] = "((stock >= 0))"
    if unique:
        table.unique.append(("sku",))
    return Schema(tables={"items": table})


# -- check_sql shapes -----------------------------------------------------


@pytest.mark.parametrize(
    "kind,kwargs,fragment",
    [
        ("non_negative", {"column": "stock"}, "< 0"),
        ("conservation", {"column": "balance", "expected_total": 50}, "SUM("),
        ("duplicate_insert", {"column": None, "key_columns": ["email"]}, "COUNT(*) > 1"),
        ("single_use", {"column": None, "key_columns": ["token"]}, "COUNT(*) > 1"),
        ("aggregate_limit", {"column": None, "key_columns": ["team_id"], "limit": 5}, "HAVING"),
    ],
)
def test_every_kind_has_a_check(kind, kwargs, fragment):
    sql, _params, predicate = check_sql_for(kind, "t", **kwargs)
    assert fragment in sql
    assert predicate


def test_check_sql_returns_rows_only_on_violation():
    """The one convention in the whole system, stated once and tested here."""
    sql, _, _ = check_sql_for("non_negative", "items", "stock")
    assert "WHERE" in sql and "< 0" in sql


# -- source precedence ----------------------------------------------------


def test_config_beats_everything():
    target = Target(
        id="t",
        route="POST /orders",
        invariant={"kind": "non_negative", "table": "items", "column": "stock"},
    )
    invariant = from_config(target)
    assert invariant.source == "config"
    assert "raceguard.yaml" in invariant.derivation


def test_config_check_sql_is_used_verbatim():
    target = Target(
        id="t",
        route="POST /o",
        invariant={
            "kind": "conservation",
            "table": "items",
            "column": "stock",
            "check_sql": "SELECT 1 FROM items WHERE stock <> 7",
        },
    )
    assert from_config(target).check_sql == "SELECT 1 FROM items WHERE stock <> 7"


def test_check_constraint_is_read_not_inferred():
    candidate = propose_from_schema(items_schema(check=True), ["items"])
    assert candidate is not None
    assert candidate.invariant.kind == "non_negative"
    assert candidate.invariant.source == "schema"
    assert "CHECK constraint" in candidate.invariant.derivation


def test_unique_index_yields_duplicate_insert():
    candidate = propose_from_schema(items_schema(unique=True), ["items"])
    assert candidate is not None
    assert candidate.invariant.kind == "duplicate_insert"
    assert "unique index" in candidate.invariant.derivation


def test_inferred_invariant_says_it_is_a_guess():
    """A guess that does not announce itself is the worst kind."""
    candidate = propose_from_schema(items_schema(), ["items"], columns=["stock"])
    assert candidate is not None
    assert "inferred" in candidate.invariant.derivation
    assert "confirm" in candidate.invariant.derivation.lower()


def test_spec_derivation_works_with_no_model_configured():
    """The tool must be usable with no model at all (section 22, question 4)."""
    candidate = propose_from_spec(
        "An order must never reduce stock below zero.",
        items_schema(),
        ["items"],
        settings=SynthConfig(provider="none"),
    )
    assert candidate is not None
    assert candidate.invariant.kind == "non_negative"
    assert candidate.used_model is False
    assert "no model configured" in candidate.invariant.derivation


# -- the gate -------------------------------------------------------------


def test_gate_rejects_an_unknown_table():
    invariant = Invariant(
        kind="non_negative", table="ghosts", column="x", predicate="p",
        check_sql="SELECT 1", source="spec", derivation="d",
    )
    with pytest.raises(InvariantRejected) as excinfo:
        validate(invariant, items_schema())
    assert excinfo.value.step == 1


def test_gate_rejects_an_unknown_column():
    invariant = Invariant(
        kind="non_negative", table="items", column="nope", predicate="p",
        check_sql="SELECT 1", source="spec", derivation="d",
    )
    with pytest.raises(InvariantRejected) as excinfo:
        validate(invariant, items_schema())
    assert excinfo.value.step == 1
    assert "does not exist" in excinfo.value.message


def test_gate_without_a_database_does_not_claim_steps_2_to_4_passed():
    """Skipping a step and passing it are different things, and a report that
    conflates them is how an unvalidated invariant reaches the harness."""
    invariant = Invariant(
        kind="non_negative", table="items", column="stock", predicate="p",
        check_sql="SELECT * FROM items WHERE stock < 0", source="schema", derivation="d",
    )
    report = validate(invariant, items_schema())
    skipped = [step for step in report.steps if "skipped" in step[3]]
    assert len(skipped) == 3


def test_corruption_recipe_respects_not_null_columns():
    """`INSERT ... DEFAULT VALUES` fails on any NOT NULL column without a
    default, and a step 4 that cannot build its counterexample proves nothing."""
    invariant = Invariant(
        kind="non_negative", table="items", column="stock", predicate="p",
        check_sql="SELECT 1", source="schema", derivation="d",
    )
    statements = build_corruption(invariant, items_schema())
    assert statements
    insert = statements[0][0]
    assert '"sku"' in insert and '"stock"' in insert
    assert "DEFAULT VALUES" not in insert


def test_corruption_for_duplicate_insert_makes_duplicates():
    invariant = Invariant(
        kind="duplicate_insert", table="items", column="sku", predicate="p",
        check_sql="SELECT 1", source="schema", derivation="d",
    )
    statements = build_corruption(invariant, items_schema(unique=True))
    assert statements and len(statements) >= 2


# -- the critic -----------------------------------------------------------


def test_critic_never_sees_the_proposers_reasoning():
    """A critic primed with the proposer's justification agrees with it. That
    is the entire reason this is a separate call with a separate prompt."""
    invariant = Invariant(
        kind="non_negative", table="items", column="stock", predicate="p",
        check_sql="SELECT 1", source="spec",
        derivation="I chose non_negative because the spec said 'below zero'",
        critic_note="earlier note",
    )
    blinded = strip_reasoning(invariant)
    assert blinded.derivation == ""
    assert blinded.critic_note is None
    assert invariant.derivation  # the original is untouched


def test_critic_is_a_no_op_without_a_model():
    verdict = criticise(
        Invariant(
            kind="non_negative", table="items", column="stock", predicate="p",
            check_sql="SELECT 1", source="spec", derivation="d",
        ),
        "spec",
        items_schema(),
        settings=SynthConfig(provider="none"),
    )
    assert verdict.sound and not verdict.ran
