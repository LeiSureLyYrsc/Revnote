"""Port reservation must survive a just-released listener without weakening exclusivity.

The web E2E runner restarts the owned API on the same port
(``durability.spec.ts`` -> ``api_restart_recovery``). The predecessor served real
requests, so on POSIX its closed connections stay in ``TIME_WAIT`` on
``127.0.0.1:<port>`` and a plain ``bind`` fails with ``EADDRINUSE`` even though
nothing is listening. A reservation that lacks ``SO_REUSEADDR`` therefore worked on
Windows (different bind semantics) and failed on Linux, taking the remaining
thirteen cases down with it.

These tests need no database and no child process: they drive the runner's own
``reserve_port`` against real sockets, so the platform behaviour is the one the CI
job will see rather than a mock of it.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_web_e2e.py"


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    """Load the runner as a module (it is a script, not an importable package)."""
    spec = importlib.util.spec_from_file_location("revnote_web_e2e_runner_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _live_listener(reuse_addr: bool = False) -> tuple[socket.socket, int]:
    """A listening socket on an OS-assigned loopback port, standing in for a rival.

    ``reuse_addr`` builds the hostile Windows case: two sockets that both asked for
    ``SO_REUSEADDR`` may share one live port there, so a reservation must never set
    that option on Windows. A plain holder is the ordinary competing server.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if reuse_addr:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return listener, listener.getsockname()[1]


def test_reserve_port_reclaims_a_port_its_predecessor_served_and_released(
    runner: ModuleType,
) -> None:
    """The exact restart path: listen, serve one client, release, reserve again.

    The server closes the accepted connection first, which is what leaves the
    server end of the pair in ``TIME_WAIT`` on Linux. The port must still be
    reservable, or an API restart can never come back up.
    """
    first = runner.reserve_port(0)
    port = first.getsockname()[1]
    first.settimeout(5)
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    client.settimeout(5)
    try:
        accepted, _peer = first.accept()
        accepted.close()  # server side closes first -> its end enters TIME_WAIT
        assert client.recv(1) == b""  # the close really reached the peer
    finally:
        client.close()
        first.close()

    again = runner.reserve_port(port)
    try:
        assert again.getsockname()[1] == port
        # Assert the mechanism, not only the timing outcome: without this option a
        # POSIX bind would depend on TIME_WAIT having already expired.
        if sys.platform == "win32":
            assert again.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) == 1
            # The hazard option must be absent: on Windows two SO_REUSEADDR sockets
            # may share a live port (measured), so it is never set here.
            assert again.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
        else:
            assert again.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 1
            if hasattr(socket, "SO_REUSEPORT"):
                # Cross-TIME_WAIT, never port sharing: a second live owner is refused.
                assert again.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT) == 0
    finally:
        again.close()


def test_reserve_port_still_refuses_a_live_listener(runner: ModuleType) -> None:
    """``SO_REUSEADDR`` crosses TIME_WAIT only; a live owner stays a hard failure.

    Widening the option to ``SO_REUSEPORT`` would let two processes serve one port,
    so this test pins the exclusivity the runner promises: a socket that is still
    listening is refused, with the original fixed message and nothing leaked.
    """
    live, port = _live_listener()
    started = time.monotonic()
    try:
        with pytest.raises(runner.LifecycleError) as caught:
            runner.reserve_port(port)
        assert str(caught.value) == ("A requested HTTP port is already occupied; refusing reuse.")
    finally:
        live.close()
    # The retry absorbs a release delay; it must never become an unbounded wait.
    assert time.monotonic() - started < 5.0

    # Once the real owner is gone the same port is reservable, so the refusal
    # above tracked the listener rather than a permanently poisoned port.
    reclaimed = runner.reserve_port(port)
    reclaimed.close()


def test_reserve_port_refuses_a_live_listener_that_also_set_reuseaddr(
    runner: ModuleType,
) -> None:
    """The Windows hazard this option split exists to avoid, pinned as a test.

    Measured on Windows: with ``SO_REUSEADDR`` on *both* sockets a second bind
    succeeds on a port that is still being served, which is exactly the silent
    hijack ``SO_EXCLUSIVEADDRUSE`` prevents. The reservation therefore sets
    ``SO_REUSEADDR`` on POSIX only. This holder mimics a rival that set it (a
    plausible configuration for any other server in the suite), so on Windows the
    reservation must still lose; on POSIX ``SO_REUSEADDR`` never implies sharing,
    so the same expectation holds. Both platforms asserting one behaviour is what
    makes the platform split safe rather than merely convenient.
    """
    live, port = _live_listener(reuse_addr=True)
    assert live.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 1
    try:
        with pytest.raises(runner.LifecycleError) as caught:
            runner.reserve_port(port)
        assert str(caught.value) == ("A requested HTTP port is already occupied; refusing reuse.")
    finally:
        live.close()


def test_the_release_retry_is_bounded(runner: ModuleType) -> None:
    """A retry exists, but it is a fixed small budget, not an open-ended poll."""
    assert 1 <= runner.PORT_RESERVATION_ATTEMPTS <= 20
    assert 0 < runner.PORT_RESERVATION_RETRY_SECONDS <= 0.25
    budget = (runner.PORT_RESERVATION_ATTEMPTS - 1) * runner.PORT_RESERVATION_RETRY_SECONDS
    assert budget <= 5.0
