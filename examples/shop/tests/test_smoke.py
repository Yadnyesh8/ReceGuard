"""Import and wiring smoke test: no database required.

This does not exercise any race -- that is what `raceguard verify` is for
(see README.md). It just proves the app imports, every route is registered,
and /health responds, so a broken import never gets mistaken for a finding.
"""

from __future__ import annotations

import httpx
import pytest

from shop.main import app

EXPECTED_ROUTES = {
    ("POST", "/orders"),
    ("POST", "/wallet/transfer"),
    ("POST", "/coupons/redeem"),
    ("POST", "/signup"),
    ("POST", "/teams/{team_id}/members"),
    ("POST", "/products/{product_id}/purchase"),
    ("GET", "/health"),
}


def test_app_imports() -> None:
    assert app.title == "RaceGuard reference shop"


def test_all_routes_registered() -> None:
    # Walk the OpenAPI schema rather than app.routes: FastAPI's internal
    # route representation (flat APIRoute vs. a lazy _IncludedRouter) has
    # changed across versions, but the generated schema is a stable,
    # public way to ask "what did include_router() actually wire up".
    schema = app.openapi()
    found = set()
    for path, methods in schema["paths"].items():
        for method in methods:
            found.add((method.upper(), path))

    missing = EXPECTED_ROUTES - found
    assert not missing, f"missing routes: {missing}"


@pytest.mark.asyncio
async def test_health_responds() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
