"""Async database plumbing: one engine, one sessionmaker, one dependency.

DATABASE_URL must be an asyncpg URL, e.g.:
    postgresql+asyncpg://user:pass@host:5432/dbname

RaceGuard points this at a fresh trial database for every trial (section 8.1);
nothing here should cache state across requests beyond the connection pool.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/shop"
)

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)

# Exported directly (not just via the FastAPI dependency) because shop/routes/teams.py
# opens its own session: its handler is a plain `def`, so it cannot use Depends(get_session)
# the way the async handlers do. See the docstring on teams.add_team_member.
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session
