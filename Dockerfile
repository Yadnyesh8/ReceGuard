# RaceGuard itself, for CI users who would rather not install Python tooling.
#
# This image needs access to a Docker socket to do its job, which is a real
# privilege. Mount it deliberately:
#
#   docker run --rm \
#     -v /var/run/docker.sock:/var/run/docker.sock \
#     -v "$PWD:/src:ro" -w /src \
#     ghcr.io/raceguard/raceguard scan .
#
# `scan` needs no socket at all. Only `verify` and `fix --apply` do.
#
# The same image hosts the MCP server:
#
#   docker run --rm -p 8765:8765 -e RACEGUARD_MCP_TRANSPORT=http \
#     -e RACEGUARD_MCP_HOST=0.0.0.0 -e RACEGUARD_MCP_TOKENS=s3cret \
#     -v "$PWD:/src" ghcr.io/raceguard/raceguard mcp serve

FROM python:3.12-slim AS build

WORKDIR /build
COPY pyproject.toml README.md ./
COPY raceguard ./raceguard

RUN pip install --no-cache-dir build && python -m build --wheel


FROM python:3.12-slim

LABEL org.opencontainers.image.title="RaceGuard" \
      org.opencontainers.image.description="Find, prove, and repair atomicity violations in Python HTTP handlers." \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/raceguard/raceguard"

# Just the docker CLI binary, so container mode can talk to a mounted socket.
# `apt-get install docker.io` drags in the whole daemon and roughly triples the
# image; this image is a client and never runs a daemon.
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /build/dist/*.whl /tmp/
# The extras cannot be applied to a glob, so resolve the filename first.
RUN WHEEL="$(ls /tmp/raceguard-*.whl)" \
    && pip install --no-cache-dir "${WHEEL}[harness,mcp]" \
    && rm -f /tmp/*.whl

# Runs unprivileged. Add the user to the host's docker group at run time when
# container mode is needed.
RUN useradd --create-home --shell /bin/bash raceguard
USER raceguard
WORKDIR /src

ENTRYPOINT ["raceguard"]
CMD ["--help"]
