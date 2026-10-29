"""A real TCP server for the transport tier: uvicorn on a loopback socket, plus a raw HTTP client.

Why not ``ASGITransport``
-------------------------
``ASGITransport`` calls the app in-process and can *never* deliver
``http.disconnect``: its ``receive`` waits for the response to complete and then
returns a disconnect message, i.e. only after the handler has already finished.
So it cannot prove anything about a client that vanishes mid-batch — and a fake
that injects ``http.disconnect`` by hand would only test the fake.

This module therefore drives the real stack:

* **Real server.** :class:`Probe` starts ``uvicorn.Server`` on a socket it binds
  itself (``127.0.0.1:0``, so the port is whatever the OS hands out) and serves the
  production app object. No import string, no reload, ``access_log=False`` so the
  test output never contains a request line, a token or a note body.
* **Real client.** Requests are written as bytes over ``asyncio.open_connection``,
  and :func:`reset` closes the socket with ``SO_LINGER (1, 0)`` so the peer gets a
  **TCP RST** rather than a polite ``FIN``. That is the closest a test can get to
  "the client's process was killed", and it is what makes uvicorn mark the cycle
  disconnected.
* **Bounded cleanup.** Every probe is an async context manager that sets
  ``should_exit``, waits for the server task, closes the listening socket and (on
  error) still disposes the app's engine through the lifespan shutdown.

Nothing here asserts; it only transports. The cases live in ``test_transport.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import struct
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from fastapi import FastAPI

__all__ = [
    "Probe",
    "RawResponse",
    "build_request",
    "open_connection",
    "open_probe",
    "push_request",
    "read_response",
    "request",
    "reset",
    "send_raw",
]

#: Actions the client can take after writing a request.
_READ_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True)
class RawResponse:
    """A parsed HTTP/1.1 response read off the socket."""

    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        import json

        return json.loads(self.body)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


@dataclass(slots=True)
class Probe:
    """A running uvicorn server bound to a loopback port."""

    app: FastAPI
    host: str
    port: int
    server: uvicorn.Server
    task: asyncio.Task[None] = field(repr=False)
    _socket: socket.socket = field(repr=False)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


async def _free_socket(host: str) -> socket.socket:
    """A bound, listening socket on an OS-assigned port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.listen(64)
    sock.setblocking(False)
    return sock


async def _wait_listening(probe: Probe, *, limit_s: float = 30.0) -> None:
    """Poll the TCP socket until the server accepts; fail rather than guess a delay."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while True:
        try:
            _reader, writer = await asyncio.open_connection(probe.host, probe.port)
        except OSError:
            writer = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        if loop.time() >= deadline:
            raise AssertionError(f"uvicorn did not start listening on {probe.base_url}")
        await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def open_probe(app: FastAPI, *, host: str = "127.0.0.1") -> AsyncIterator[Probe]:
    """Serve ``app`` over a real socket for the duration of the block.

    ``access_log=False`` and ``log_level="warning"`` are deliberate: the access log
    writes the request line (which carries the workspace id) and an error log can
    quote a body. Diagnostics come from the assertions, not from the server log.
    """
    sock = await _free_socket(host)
    port = int(sock.getsockname()[1])
    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        access_log=False,
        log_level="warning",
        lifespan="on",
        # One event loop, no signal-driven reload, no workers: the server must be a
        # task in *this* loop so the test can await its shutdown deterministically.
        loop="asyncio",
        workers=1,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]), name="uvicorn-probe")
    probe = Probe(app=app, host=host, port=port, server=server, task=task, _socket=sock)
    try:
        await _wait_listening(probe)
        yield probe
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=30.0)
        except TimeoutError:  # pragma: no cover - hung shutdown
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        with contextlib.suppress(OSError):
            sock.close()


def build_request(
    *,
    method: str,
    path: str,
    body: bytes | None,
    token: str | None,
    host: str = "127.0.0.1",
    port: int = 0,
    extra_headers: Mapping[str, str] | None = None,
) -> bytes:
    """A raw HTTP/1.1 request, byte for byte (no client library involved).

    ``token=None`` omits the ``Authorization`` header entirely (login has no token,
    and sending an empty ``Bearer`` would be a different request). ``Connection:
    close`` keeps the response ending at EOF, so the reader needs no chunked
    decoding and a truncated response is unambiguous.
    """
    headers: dict[str, str] = {
        "Host": f"{host}:{port}",
        "Accept": "application/json",
        "Connection": "close",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(body))
    headers.update(extra_headers or {})
    head = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
    return f"{method} {path} HTTP/1.1\r\n{head}\r\n".encode("latin-1") + (body or b"")


def push_request(
    *,
    token: str,
    workspace_id: uuid.UUID,
    ops: Sequence[Mapping[str, Any]],
    host: str = "127.0.0.1",
    port: int = 0,
) -> bytes:
    """The concrete push request: ``{"ops": [...]}`` on the wire."""
    body = json.dumps({"ops": list(ops)}, separators=(",", ":")).encode("utf-8")
    return build_request(
        method="POST",
        path=f"/api/v1/workspaces/{workspace_id}/sync/push",
        body=body,
        token=token,
        host=host,
        port=port,
    )


async def open_connection(probe: Probe) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A TCP connection to the probe (the caller writes and closes it)."""
    return await asyncio.open_connection(probe.host, probe.port)


async def send_raw(
    probe: Probe, payload: bytes
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a connection and write ``payload``; the caller decides what happens next."""
    reader, writer = await open_connection(probe)
    writer.write(payload)
    await writer.drain()
    return reader, writer


async def request(
    probe: Probe,
    payload: bytes,
    *,
    limit_s: float = _READ_TIMEOUT,
) -> RawResponse:
    """Send one request and read its response to EOF (the ordinary path)."""
    reader, writer = await send_raw(probe, payload)
    try:
        return await read_response(reader, limit_s=limit_s)
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def read_response(
    reader: asyncio.StreamReader, *, limit_s: float = _READ_TIMEOUT
) -> RawResponse:
    """Read one HTTP/1.1 response (``Connection: close`` semantics)."""
    return await asyncio.wait_for(_read_response(reader), timeout=limit_s)


async def _read_response(reader: asyncio.StreamReader) -> RawResponse:
    raw = await reader.read(-1)
    if not raw:
        raise AssertionError("the server closed the connection without a response")
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ", 2)[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers[name.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    return RawResponse(status=status, headers=headers, body=body)


def reset(writer: asyncio.StreamWriter) -> None:
    """Abort the TCP connection so the peer receives an RST, not a FIN.

    ``SO_LINGER`` with a zero timeout makes ``close()`` discard the send buffer and
    send a reset segment. uvicorn's protocol then reports ``connection_lost`` while
    the request cycle is incomplete, which is the state a real client crash leaves
    behind — the only state in which "the client went away" is a fact.
    """
    sock = writer.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    with contextlib.suppress(Exception):
        writer.close()
    # No ``await writer.wait_closed()``: the whole point is not to wait for a FIN.
    del sock
