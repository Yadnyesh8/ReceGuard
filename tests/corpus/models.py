"""Shared SQLAlchemy models for the RaceGuard test corpus.

Every fixture handler imports the models it needs from this module. Fixtures
are meant to be readable in isolation, not executed as a live service, so the
engine/session wiring inside each fixture is illustrative rather than a real
app factory.

Deliberate design note: several scenarios exist in TWO shapes, e.g.
``Signup`` (no unique constraint) versus ``UniqueSignup`` (unique constraint
on the natural key). This is intentional and load-bearing for the corpus
labels: a ``duplicate_insert`` fixture is only genuinely vulnerable to a
duplicate row appearing in Postgres if nothing at the schema level rules
that out. Pairing an unconstrained table with a constrained twin lets the
vulnerable/ and safe/ fixtures both be honest about what the code (plus
schema) actually guarantees, rather than one of them being unfalsifiable.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Item(Base):
    """A stock-tracked catalog item. Backs the oversell fixtures."""

    __tablename__ = "items"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    stock = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow)


class Account(Base):
    """A wallet-style balance. No constraint enforces conservation across
    rows; that invariant lives entirely in handler logic, which is exactly
    what makes it raceable."""

    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    owner = Column(String, nullable=False)
    balance = Column(Numeric, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow)


class Coupon(Base):
    """A single-use coupon tracked with a boolean flag."""

    __tablename__ = "coupons"

    id = Column(Integer, primary_key=True)
    code = Column(String, nullable=False)
    redeemed = Column(Boolean, nullable=False, default=False)


class UniqueCouponRedemption(Base):
    """One redemption row per (coupon, user), enforced at the schema level.
    Backs the unique_constraint-guarded single_use fixtures."""

    __tablename__ = "unique_coupon_redemptions"

    id = Column(Integer, primary_key=True)
    coupon_id = Column(Integer, ForeignKey("coupons.id"), nullable=False)
    user_id = Column(Integer, nullable=False)

    __table_args__ = (UniqueConstraint("coupon_id", "user_id", name="uq_coupon_user"),)


class Invite(Base):
    """A one-time invite token, consumed via a boolean flag."""

    __tablename__ = "invites"

    id = Column(Integer, primary_key=True)
    token = Column(String, nullable=False, unique=True)
    used = Column(Boolean, nullable=False, default=False)


class Signup(Base):
    """No unique constraint on email: the duplicate_insert vulnerable
    fixtures rely on that absence."""

    __tablename__ = "signups"

    id = Column(Integer, primary_key=True)
    email = Column(String, nullable=False)


class UniqueSignup(Base):
    """Same shape as Signup, but email is unique at the schema level."""

    __tablename__ = "unique_signups"

    id = Column(Integer, primary_key=True)
    email = Column(String, nullable=False, unique=True)


class Team(Base):
    __tablename__ = "teams"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    member_limit = Column(Integer, nullable=False, default=5)


class TeamMember(Base):
    """No uniqueness or count constraint: the 5-per-team rule is an
    application-level aggregate_limit invariant, not a schema one."""

    __tablename__ = "team_members"

    id = Column(Integer, primary_key=True)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False)
    user_id = Column(Integer, nullable=False)


class Order(Base):
    """No unique constraint on idempotency_key: backs the lookup-then-act
    idempotency race. See IdempotentOrder for the claim-first fix."""

    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    idempotency_key = Column(String, nullable=False)
    total = Column(Numeric, nullable=False, default=0)
    status = Column(String, nullable=False, default="pending")


class IdempotentOrder(Base):
    """Same shape as Order, but idempotency_key is unique at the schema
    level, which is what makes ON CONFLICT DO NOTHING meaningful."""

    __tablename__ = "idempotent_orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    idempotency_key = Column(String, nullable=False, unique=True)
    total = Column(Numeric, nullable=False, default=0)
    status = Column(String, nullable=False, default="pending")


class Follow(Base):
    """No unique constraint on (follower, followee): backs the
    duplicate_insert follow-relationship fixture."""

    __tablename__ = "follows"

    id = Column(Integer, primary_key=True)
    follower_id = Column(Integer, nullable=False)
    followee_id = Column(Integer, nullable=False)


class PendingReview(Base):
    """No unique constraint on (user, product): backs the vulnerable
    one-review-per-product fixture. See Review for the guarded twin."""

    __tablename__ = "pending_reviews"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    product_id = Column(Integer, nullable=False)
    rating = Column(Integer, nullable=False)


class Review(Base):
    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    product_id = Column(Integer, nullable=False)
    rating = Column(Integer, nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "product_id", name="uq_review_user_product"),)


class UserSession(Base):
    """Backs the aggregate_limit 'at most N active sessions' fixtures."""

    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    active = Column(Boolean, nullable=False, default=True)


class CreditAccount(Base):
    __tablename__ = "credit_accounts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, unique=True)
    credit_limit = Column(Numeric, nullable=False)


class PendingCharge(Base):
    """Backs the aggregate_limit credit-limit fixture: the sum of a user's
    pending charges must stay under their credit_limit."""

    __tablename__ = "pending_charges"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    amount = Column(Numeric, nullable=False)


class WebhookEvent(Base):
    """No unique constraint on external_id: backs the suppressed
    duplicate_insert fixture (dedup handled by an external service the
    scanner cannot see)."""

    __tablename__ = "webhook_events"

    id = Column(Integer, primary_key=True)
    external_id = Column(String, nullable=False)
    processed = Column(Boolean, nullable=False, default=False)
