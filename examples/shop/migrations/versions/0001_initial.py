"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-31

Deliberately vulnerable schema for the RaceGuard reference app. Five tables
back the five vulnerable endpoints and are missing, on purpose, the exact
constraint that would make each bug impossible at the database level:

  - items.stock has no CHECK(stock >= 0)               -> orders.py oversell
  - accounts.balance has no CHECK(balance >= 0)         -> wallet.py lost update
  - coupon_redemptions has no unique index on coupon_id -> coupons.py single_use
  - users.email has NO UNIQUE INDEX at all              -> signup.py duplicate_insert
  - team_members has no constraint capping rows per team_id -> teams.py aggregate_limit

To give RaceGuard's schema-reflection invariant derivation something real to
find, teams.max_members DOES carry a genuine CHECK constraint
(CHECK (max_members > 0)). It sits on the table used by the aggregate_limit
race but does not guard the race itself (the race is on the *count* of
team_members rows, not on teams.max_members), so it does not accidentally
close the vulnerability it sits next to.

products.stock backs the one CORRECTLY guarded endpoint (safe.py); it needs
no constraint because the handler's atomic UPDATE ... WHERE stock >= qty
already makes negative stock unreachable.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No constraint ties orders back to items.stock. That is deliberate: the
    # conservation invariant, not the schema, is what catches the oversell.
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
    )
    op.create_table(
        "items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=False),
        # No CHECK(stock >= 0) -- see module docstring.
    )

    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("balance", sa.Integer(), nullable=False),
        # No CHECK(balance >= 0) -- see module docstring.
    )

    op.create_table(
        "coupons",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("max_redemptions", sa.Integer(), nullable=False, server_default="1"),
    )

    op.create_table(
        "coupon_redemptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "coupon_id", sa.Integer(), sa.ForeignKey("coupons.id"), nullable=False
        ),
        sa.Column("redeemed_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # No unique index on coupon_id -- see module docstring.
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=256), nullable=False),
        # No UNIQUE INDEX on email -- see module docstring.
    )

    op.create_table(
        "teams",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("max_members", sa.Integer(), nullable=False, server_default="5"),
        sa.CheckConstraint("max_members > 0", name="ck_teams_max_members_positive"),
    )

    op.create_table(
        "team_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("user_email", sa.String(length=256), nullable=False),
        # No constraint capping rows per team_id -- see module docstring.
    )

    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("products")
    op.drop_table("team_members")
    op.drop_table("teams")
    op.drop_table("users")
    op.drop_table("coupon_redemptions")
    op.drop_table("coupons")
    op.drop_table("accounts")
    op.drop_table("items")
