"""Static bearer tokens for the HTTP transport.

Implements the SDK's ``TokenVerifier`` protocol so an operator who wants JWT
or an introspection endpoint swaps this class and nothing else. Comparison is
constant time and tokens never reach a log line.

``RACEGUARD_MCP_TOKENS`` is comma separated; each entry is ``token`` or
``token:scope1+scope2`` (scopes joined with ``+`` so the comma stays the entry
separator). The shipped tools do not gate on scopes; they are recorded on the
``AccessToken`` for a downstream verifier or middleware that does.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from mcp.server.auth.provider import AccessToken, TokenVerifier


def parse_tokens(entries: list[str]) -> dict[str, list[str]]:
    """``["abc:read+verify", "def"]`` -> ``{"abc": ["read", "verify"], "def": []}``."""
    parsed: dict[str, list[str]] = {}
    for entry in entries:
        token, _, scopes = entry.partition(":")
        token = token.strip()
        if not token:
            continue
        parsed[token] = [s for s in scopes.split("+") if s]
    return parsed


class StaticTokenVerifier(TokenVerifier):
    def __init__(self, entries: list[str]) -> None:
        self._tokens = parse_tokens(entries)

    @property
    def configured(self) -> bool:
        return bool(self._tokens)

    async def verify_token(self, token: str) -> AccessToken | None:
        return self.verify(token)

    def verify(self, token: str) -> AccessToken | None:
        for known, scopes in self._tokens.items():
            if hmac.compare_digest(known.encode(), token.encode()):
                return AccessToken(
                    token=token,
                    client_id=client_id_for(token),
                    scopes=scopes,
                    expires_at=None,
                    subject=client_id_for(token),
                )
        return None


def client_id_for(token: str) -> str:
    """A stable, non-reversible identity for budgets and logs."""
    return "tok_" + hashlib.sha256(token.encode()).hexdigest()[:12]


def unix_now() -> int:
    return int(time.time())
