"""FastAPI app factory for the RaceGuard reference shop.

Five vulnerable endpoints (orders, wallet, coupons, signup, teams) and one
correctly guarded one (safe/products), wired up behind a plain /health check
so both container mode and inprocess mode (section 8.2) can boot this the
same way.
"""

from __future__ import annotations

from fastapi import FastAPI

from shop.routes import coupons, orders, safe, signup, teams, wallet


def create_app() -> FastAPI:
    app = FastAPI(title="RaceGuard reference shop", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(orders.router)
    app.include_router(wallet.router)
    app.include_router(coupons.router)
    app.include_router(signup.router)
    app.include_router(teams.router)
    app.include_router(safe.router)

    return app


app = create_app()
