# RaceGuard itself, for CI users who would rather not install Python tooling.
#
#   docker run --rm -v "$PWD:/src:ro" -w /src ghcr.io/yadnyesh8/receguard scan .
#
# `verify` and `fix --apply` do not run from this image. They execute your
# migrate command, which needs your app's own tools, and reach the app and its
# database through the host. Install raceguard-cli[harness] beside the app.
#
# The same image hosts the MCP server:
#
#   docker run --rm -p 8765:8765 -e RACEGUARD_MCP_TRANSPORT=http \
#     -e RACEGUARD_MCP_HOST=0.0.0.0 -e RACEGUARD_MCP_TOKENS=s3cret:verify \
#     -e RACEGUARD_MCP_VERIFY=false \
#     -v "$PWD:/src" ghcr.io/yadnyesh8/receguard mcp serve

FROM python:3.12-slim AS build

WORKDIR /build
COPY pyproject.toml README.md ./
COPY raceguard ./raceguard
# The wheel force-includes docs/, so the build fails without it.
COPY docs ./docs

RUN pip install --no-cache-dir build && python -m build --wheel


FROM python:3.12-slim

LABEL org.opencontainers.image.title="RaceGuard" \
      org.opencontainers.image.description="Find, prove, and repair atomicity violations in Python HTTP handlers." \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/Yadnyesh8/ReceGuard"

RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /build/dist/*.whl /tmp/
# The extras cannot be applied to a glob, so resolve the filename first.
RUN WHEEL="$(ls /tmp/raceguard_cli-*.whl)" \
    && pip install --no-cache-dir "${WHEEL}[harness,mcp]" \
    && rm -f /tmp/*.whl

RUN useradd --create-home --shell /bin/bash raceguard
USER raceguard
WORKDIR /src

ENTRYPOINT ["raceguard"]
CMD ["--help"]
