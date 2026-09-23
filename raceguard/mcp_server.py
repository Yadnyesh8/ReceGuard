"""Compatibility shim. The server lives in ``raceguard.mcp`` now.

``python -m raceguard.mcp_server`` and ``from raceguard.mcp_server import mcp``
keep working for hosts configured against the old module path.
"""

from __future__ import annotations

from .mcp.server import SDK_MAJOR, build, main
from .mcp.settings import MCPSettings

mcp, _runtime = build(MCPSettings.from_env())

__all__ = ["SDK_MAJOR", "main", "mcp"]

if __name__ == "__main__":
    main()
