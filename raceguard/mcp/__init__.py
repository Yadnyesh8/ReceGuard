"""The MCP adapter: stdio for editors, streamable HTTP for hosting.

``build(settings)`` returns a fully registered server; ``serve(settings)``
runs it. See docs/ci-and-mcp-spec.md section 4 for the contract.
"""

from .server import SDK_MAJOR, build, build_http_app, serve
from .settings import MCPSettings

__all__ = ["MCPSettings", "SDK_MAJOR", "build", "build_http_app", "serve"]
