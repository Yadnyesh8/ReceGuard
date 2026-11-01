# The hosted server with the shop example inside it, built from a clone of this
# repository with the repository root as the context. A host with no Docker
# socket cannot verify, so this leaves out the harness and the docker CLI.
FROM python:3.12-slim

WORKDIR /build
COPY pyproject.toml README.md ./
COPY raceguard ./raceguard
# The wheel force-includes docs/, so the build fails without it.
COPY docs ./docs
RUN pip install --no-cache-dir ".[mcp]" && rm -rf /build

RUN useradd --create-home --shell /bin/bash raceguard
COPY --chown=raceguard examples/shop /src
USER raceguard
WORKDIR /src

ENV RACEGUARD_MCP_TRANSPORT=http \
    RACEGUARD_MCP_HOST=0.0.0.0 \
    RACEGUARD_MCP_ROOT=/src \
    RACEGUARD_MCP_VERIFY=false

EXPOSE 10000
ENTRYPOINT ["raceguard"]
CMD ["mcp", "serve"]
