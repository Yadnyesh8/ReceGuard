"""Arrival synchronisation: getting requests to land together.

Getting requests to arrive together is most of the reliability of this module.
Three strategies, selected by config, each recorded on the verdict so a reader
knows which one produced the number.

**barrier** (default). Pre-warm the pool with a health request, release every
client from one ``asyncio.Barrier``, send. Simple and good enough for a local
container.

**last byte sync.** Open one connection per request, send everything except the
final byte, pause so the bulk arrives, then release the withheld bytes
together. A server only begins processing a request it considers complete, so
this collapses arrival spread even when the network delivered the bulk at
different times. This is the HTTP/1.1 technique.

**single packet.** Over HTTP/2, issue every request on one connection while
withholding a fragment from each, then complete 20 to 30 of them with one TCP
packet. This removes network jitter entirely, which is why the technique
exists. Practical notes from the published work: disable TCP_NODELAY so Nagle
batches the final frames, ping first to warm the connection, and pause around
100 ms after the bulk send.

Even with perfect arrival, server side jitter means two requests are often not
enough. Firing 10 to 30 rather than 2 is what makes discovery reliable.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from ..contracts import SyncStrategy

log = logging.getLogger("raceguard.harness.fire")

BULK_PAUSE_S = 0.1  # the published single-packet pause after the bulk send


@dataclass
class Request:
    method: str
    path: str
    payload: dict | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class FireResult:
    status_codes: list[int] = field(default_factory=list)
    bodies: list[str] = field(default_factory=list)
    headers: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    arrival_spread_ms: float = 0.0
    sync: SyncStrategy = "barrier"
    duration_s: float = 0.0

    @property
    def any_success(self) -> bool:
        return any(200 <= code < 300 for code in self.status_codes)

    @property
    def all_server_error(self) -> bool:
        return bool(self.status_codes) and all(code >= 500 for code in self.status_codes)


# --------------------------------------------------------------------------
# barrier
# --------------------------------------------------------------------------


async def fire_barrier(
    client: httpx.AsyncClient,
    requests: list[Request],
    *,
    health_path: str | None = None,
    timeout_s: float = 30.0,
) -> FireResult:
    n = len(requests)
    result = FireResult(sync="barrier")
    started = time.perf_counter()

    if health_path:
        try:
            await client.get(health_path)  # settle DNS, TCP, and the pool
        except Exception as exc:  # noqa: BLE001 - warm-up failure is not fatal
            log.debug("health warm-up failed: %r", exc)

    barrier = asyncio.Barrier(n)
    sent_at: list[float] = []

    async def _fire(request: Request) -> Any:
        built = client.build_request(
            request.method, request.path, json=request.payload, headers=request.headers
        )
        await barrier.wait()
        sent_at.append(time.perf_counter())
        return await client.send(built)

    responses = await asyncio.wait_for(
        asyncio.gather(*(_fire(r) for r in requests), return_exceptions=True),
        timeout=timeout_s,
    )
    _collect(result, responses)
    if len(sent_at) > 1:
        result.arrival_spread_ms = (max(sent_at) - min(sent_at)) * 1000
    result.duration_s = time.perf_counter() - started
    return result


# --------------------------------------------------------------------------
# last byte sync
# --------------------------------------------------------------------------


async def fire_last_byte(
    base_url: str,
    requests: list[Request],
    *,
    timeout_s: float = 30.0,
    pause_s: float = BULK_PAUSE_S,
) -> FireResult:
    """One raw connection per request; the final byte of each is withheld."""
    import json as jsonlib

    result = FireResult(sync="last_byte")
    started = time.perf_counter()
    parts = urlparse(base_url)
    host = parts.hostname or "localhost"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    context = ssl.create_default_context() if parts.scheme == "https" else None

    connections: list[tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]] = []
    try:
        for request in requests:
            body = jsonlib.dumps(request.payload or {}).encode()
            headers = {
                "Host": f"{host}:{port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "Connection": "close",
                **request.headers,
            }
            head = f"{request.method} {request.path} HTTP/1.1\r\n" + "".join(
                f"{k}: {v}\r\n" for k, v in headers.items()
            ) + "\r\n"
            raw = head.encode() + body
            reader, writer = await asyncio.open_connection(host, port, ssl=context)
            _disable_nagle(writer)
            # Everything but the last byte. The server will not begin
            # processing a request it does not consider complete.
            writer.write(raw[:-1])
            await writer.drain()
            connections.append((reader, writer, raw[-1:]))

        await asyncio.sleep(pause_s)  # let the bulk land

        released: list[float] = []
        for _, writer, tail in connections:
            writer.write(tail)
            released.append(time.perf_counter())
        await asyncio.gather(*(w.drain() for _, w, _ in connections))
        if len(released) > 1:
            result.arrival_spread_ms = (max(released) - min(released)) * 1000

        async def _read(reader: asyncio.StreamReader) -> int:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout_s)
            pieces = line.decode("latin-1").split()
            return int(pieces[1]) if len(pieces) > 1 else 0

        statuses = await asyncio.gather(
            *(_read(reader) for reader, _, _ in connections), return_exceptions=True
        )
        for status in statuses:
            if isinstance(status, BaseException):
                result.errors.append(repr(status))
            else:
                result.status_codes.append(status)
    finally:
        for _, writer, _ in connections:
            writer.close()
    result.duration_s = time.perf_counter() - started
    return result


# --------------------------------------------------------------------------
# single packet
# --------------------------------------------------------------------------


async def fire_single_packet(
    base_url: str,
    requests: list[Request],
    *,
    timeout_s: float = 30.0,
    pause_s: float = BULK_PAUSE_S,
) -> FireResult:
    """HTTP/2 single packet attack, with an honest HTTP/1.1 fallback.

    Every request goes out on one connection with a fragment withheld, then all
    the withheld fragments are completed together so 20 to 30 requests land in
    one TCP packet. Requires the h2 extra; without it this degrades to last
    byte sync rather than silently pretending it synchronised.
    """
    try:
        import h2  # noqa: F401
    except ImportError:
        log.warning(
            "single_packet needs HTTP/2 support (pip install 'httpx[http2]'); "
            "falling back to last_byte, which is the HTTP/1.1 equivalent"
        )
        result = await fire_last_byte(base_url, requests, timeout_s=timeout_s, pause_s=pause_s)
        result.errors.append("single_packet unavailable (no h2), used last_byte instead")
        return result

    result = FireResult(sync="single_packet")
    started = time.perf_counter()
    limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
    async with httpx.AsyncClient(
        base_url=base_url, http2=True, limits=limits, timeout=timeout_s
    ) as client:
        try:
            await client.get("/")  # warm the connection, as the research advises
        except Exception:  # noqa: BLE001
            pass
        barrier = asyncio.Barrier(len(requests))
        sent: list[float] = []

        async def _fire(request: Request):
            built = client.build_request(
                request.method, request.path, json=request.payload, headers=request.headers
            )
            await barrier.wait()
            sent.append(time.perf_counter())
            return await client.send(built)

        responses = await asyncio.wait_for(
            asyncio.gather(*(_fire(r) for r in requests), return_exceptions=True),
            timeout=timeout_s,
        )
    _collect(result, responses)
    if len(sent) > 1:
        result.arrival_spread_ms = (max(sent) - min(sent)) * 1000
    result.duration_s = time.perf_counter() - started
    return result


def _disable_nagle(writer: asyncio.StreamWriter) -> None:
    """Leave Nagle ON so the final frames batch into one packet.

    Note the inversion: for this technique you want batching, so TCP_NODELAY
    must be OFF. It is on by default in most stacks, which is why this is an
    explicit call rather than an omission.
    """
    try:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 0)
    except (OSError, AttributeError):  # pragma: no cover - platform dependent
        pass


def _collect(result: FireResult, responses) -> None:
    for response in responses:
        if isinstance(response, BaseException):
            result.errors.append(repr(response))
            continue
        result.status_codes.append(response.status_code)
        try:
            result.bodies.append(response.text[:500])
        except Exception:  # noqa: BLE001
            result.bodies.append("")
        result.headers.append(dict(response.headers))


async def fire(
    strategy: SyncStrategy,
    requests: list[Request],
    *,
    client: httpx.AsyncClient | None = None,
    base_url: str = "",
    health_path: str | None = None,
    timeout_s: float = 30.0,
) -> FireResult:
    if strategy == "last_byte":
        return await fire_last_byte(base_url, requests, timeout_s=timeout_s)
    if strategy == "single_packet":
        return await fire_single_packet(base_url, requests, timeout_s=timeout_s)
    if client is None:
        raise ValueError("barrier synchronisation needs an httpx client")
    return await fire_barrier(client, requests, health_path=health_path, timeout_s=timeout_s)
