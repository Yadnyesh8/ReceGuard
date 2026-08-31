"""SQLAlchemy 2.x ORM models, declarative style with Mapped/mapped_column.

Eight small tables, one per concern. Deliberately missing the constraints
that would make the five bugs impossible (see migrations/versions/0001_initial.py
for exactly which constraints are absent, and why).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Item(Base):
    """Stock for the vulnerable /orders endpoint. No CHECK(stock >= 0)."""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    stock: Mapped[int] = mapped_column(Integer, nullable=False)


class Order(Base):
    """Every accepted order is recorded here.

    This exists so the oversell race is actually DETECTABLE. Decrementing
    `items.stock` through an ORM attribute makes two concurrent requests write
    the SAME value (1 -> 0 twice), not a negative one, so a `stock >= 0` check
    holds while two units were sold from a stock of one. Recording the orders
    gives the invariant something to conserve against:

        items.stock + SUM(orders.qty) must equal the stock we started with.
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)


class Account(Base):
    """Wallet balances for the vulnerable /wallet/transfer endpoint."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    balance: Mapped[int] = mapped_column(Integer, nullable=False)


class Coupon(Base):
    """A discount code with a redemption budget."""

    __tablename__ = "coupons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    max_redemptions: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class CouponRedemption(Base):
    """A single redemption of a coupon. No unique constraint on coupon_id."""

    __tablename__ = "coupon_redemptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    coupon_id: Mapped[int] = mapped_column(ForeignKey("coupons.id"), nullable=False)
    redeemed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc))


class User(Base):
    """A shop account. No unique index on email -- see 0001_initial.py."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(256), nullable=False)


class Team(Base):
    """A team with a membership cap. Carries the schema's one CHECK constraint."""

    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    max_members: Mapped[int] = mapped_column(Integer, nullable=False, default=5)


class TeamMember(Base):
    """A membership row. No unique or count-limiting constraint."""

    __tablename__ = "team_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    user_email: Mapped[str] = mapped_column(String(256), nullable=False)


class Product(Base):
    """Stock for the CORRECTLY guarded /products/{id}/purchase endpoint."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    stock: Mapped[int] = mapped_column(Integer, nullable=False)
