"""Own a real API/Vite/browser test run against a fresh external PostgreSQL database.

The caller owns PostgreSQL and creates a third, empty, disposable database. This
runner never starts/stops PostgreSQL, drops a database, reads a development env
file, or attaches to a pre-existing HTTP service. Diagnostics deliberately omit
child output, URLs, credentials and assertion bodies. See apps/web/e2e/README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Awaitable, Callable, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NoReturn

import asyncpg
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from starlette.requests import Request
from starlette.responses import Response

REPO_ROOT = Path(__file__).resolve().parents[3]
WEB_ROOT = REPO_ROOT / "apps" / "web"


class LifecycleError(RuntimeError):
    """A safe, fixed-message lifecycle failure (never include child output)."""


if sys.platform == "win32":

    def load_kernel32() -> ctypes.CDLL:
        """Only a Windows interpreter can load the kernel job API."""
        return ctypes.WinDLL("kernel32", use_last_error=True)

else:

    def load_kernel32() -> ctypes.CDLL:
        # Callers already construct a job only when ``os.name == "nt"``; stating
        # the precondition here keeps the win32-only ctypes members out of a
        # non-Windows type stub (and out of a Linux import path), while a future
        # caller that forgets the guard still fails with a fixed message.
        raise LifecycleError("The owned Windows process job requires Windows.")


class WindowsJob:
    """Kill every owned descendant on close, even after its leader has exited.

    A process group / taskkill of a dead pnpm leader is not sufficient on Windows.
    The kernel job retains ownership independently of that leader's lifetime.
    """

    def __init__(self) -> None:
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t),
                ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("r", "w", "o", "rb", "wb", "ob")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io", IoCounters),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t),
            ]

        self.kernel = load_kernel32()
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise LifecycleError("Could not create an owned Windows process job.")
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.close()
            raise LifecycleError("Could not configure the owned Windows process job.")

    def add(self, process: subprocess.Popen[bytes]) -> None:
        handle = self.kernel.OpenProcess(0x0101, False, process.pid)
        if not handle:
            if process.poll() is not None:
                raise LifecycleError("A child exited before process ownership was established.")
            raise LifecycleError("Could not establish owned Windows process access.")
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, handle):
                raise LifecycleError("Could not assign a child to the owned Windows job.")
        finally:
            self.kernel.CloseHandle(handle)

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def validate_database_url(raw: str) -> str:
    """Require an explicit loopback, one-time web database, distinct from other suites."""
    if not raw:
        raise LifecycleError("An explicit WEB_E2E_DATABASE_URL or --database-url is required.")
    try:
        target = make_url(raw)
        if (
            target.drivername != "postgresql+asyncpg"
            or target.host not in {"127.0.0.1", "localhost", "::1"}
            or target.query
            or not target.username
            or not re.fullmatch(r"revnote_web_e2e(?:_[a-zA-Z0-9_]+)?", target.database or "")
        ):
            raise LifecycleError("Use a loopback async PostgreSQL DSN named revnote_web_e2e[_id].")
        for key in ("DATABASE_URL", "TEST_DATABASE_URL", "TEST_MIGRATION_DATABASE_URL"):
            other_raw = os.environ.get(key)
            if not other_raw:
                continue
            other = make_url(other_raw)
            # Every accepted hostname is loopback. Credentials and hostname
            # spelling cannot disguise a collision on the same port/database.
            if (
                other.host in {"127.0.0.1", "localhost", "::1"}
                and (other.port or 5432) == (target.port or 5432)
                and other.database == target.database
            ):
                raise LifecycleError("The web test database must be separate from other databases.")
    except (ArgumentError, ValueError):
        raise LifecycleError("The explicit web test database URL is invalid.") from None
    return raw


async def require_empty_database(raw: str) -> None:
    """Inspect the real catalog before Alembic or any application write occurs."""
    url = make_url(raw)
    connection = await asyncpg.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database=url.database,
        timeout=10,
    )
    try:
        objects = await connection.fetchval(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
            "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%' "
            "AND c.relkind IN ('r','p','v','m','S','f')"
        )
        if objects:
            raise LifecycleError(
                "The web test database must be fresh, empty and exclusively owned."
            )
        # A health probe can hold a backend for a few milliseconds. Sample the
        # catalog briefly so a transient probe is not mistaken for a competing
        # client, while a persistent second owner is still refused.
        for attempt in range(12):
            others = await connection.fetchval(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname=current_database() AND pid<>pg_backend_pid()"
            )
            if not others:
                break
            if attempt == 11:
                raise LifecycleError(
                    "The web test database must be fresh, empty and exclusively owned."
                )
            await asyncio.sleep(0.25)
    finally:
        await connection.close()


#: Bounded retry that only absorbs the kernel releasing a just-killed listener.
#: Ten attempts over roughly half a second never turn a real conflict into a wait.
PORT_RESERVATION_ATTEMPTS = 10
PORT_RESERVATION_RETRY_SECONDS = 0.05


def reserve_port(requested: int) -> socket.socket:
    """Bind a loopback port we will hand to one owned child process.

    A restarted API rebinds the port its predecessor just lost. On POSIX that
    predecessor served requests, so its closed connections linger in ``TIME_WAIT``
    on ``127.0.0.1:<port>``; a plain ``bind`` then fails with ``EADDRINUSE`` even
    though nothing is listening. ``SO_REUSEADDR`` is the option that lets a bind
    cross that state. Windows has different bind semantics, so it keeps
    ``SO_EXCLUSIVEADDRUSE`` rather than mixing both rules on one socket.

    This is still exclusive: ``SO_REUSEADDR`` never permits taking a port another
    socket is *listening* on (that would be ``SO_REUSEPORT``, which is deliberately
    not used), so a live owner yields the same fixed error as before. The short
    retry only covers the gap between killing a process and the kernel releasing
    its listener; when it is exhausted the original error is raised unchanged.
    """
    for attempt in range(PORT_RESERVATION_ATTEMPTS):
        reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt":
                reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                reservation.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            reservation.bind(("127.0.0.1", requested))
            reservation.listen(1)
        except OSError:
            reservation.close()
            if attempt == PORT_RESERVATION_ATTEMPTS - 1:
                break
            time.sleep(PORT_RESERVATION_RETRY_SECONDS)
            continue
        return reservation
    raise LifecycleError("A requested HTTP port is already occupied; refusing reuse.")


def spawn(
    command: Sequence[str], env: dict[str, str], cwd: Path, job: WindowsJob | None = None
) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    if job is not None:
        try:
            job.add(process)
        except LifecycleError:
            stop_owned(process)
            raise
    return process


def stop_owned(process: subprocess.Popen[bytes]) -> None:
    """Only terminate the process group created by this invocation."""
    if os.name == "nt":
        if process.poll() is None:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=20,
            )
    else:
        # The leader can exit before its browser descendants. The session is
        # still ours, so kill the group even when poll() already has a status.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def wait_http(process: subprocess.Popen[bytes], url: str, run_id: str | None = None) -> None:
    deadline = time.monotonic() + 60
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LifecycleError("An owned HTTP server exited before becoming ready.")
        try:
            with opener.open(url, timeout=1) as response:
                if response.status == 200 and (
                    run_id is None or response.headers.get("X-Revnote-E2E-Owner") == run_id
                ):
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    raise LifecycleError("An owned HTTP server did not become ready before the deadline.")


def child_environment(database_url: str, run_id: str) -> dict[str, str]:
    """No inherited app settings can redirect a test to a development target."""
    prefixes = ("DATABASE_", "DB_", "AUTH_", "ARGON2_", "SYNC_", "SERVER_", "VITE_")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(prefixes)
        and key not in {"APP_ENV", "ENVIRONMENT", "API_PROXY_TARGET", "CORS_ALLOW_ORIGINS"}
    }
    env.update(
        DATABASE_URL=database_url,
        WEB_E2E_DATABASE_URL=database_url,
        WEB_E2E_RUN_ID=run_id,
        REQUIRE_POSTGRES_TESTS="1",
        APP_ENV="test",
        AUTH_REGISTRATION_OPEN="true",
        DB_ECHO="false",
        NO_COLOR="1",
    )
    return env


def run_checked(
    command: Sequence[str],
    env: dict[str, str],
    cwd: Path,
    owned: list[subprocess.Popen[bytes]],
    job: WindowsJob | None = None,
    timeout: int = 600,
) -> None:
    process = spawn(command, env, cwd, job)
    owned.append(process)
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise LifecycleError("An owned verification command exceeded its deadline.") from None
    if code:
        raise LifecycleError(
            f"An owned verification command exited with code {code} (output redacted)."
        )


def internal_entrypoint(args: list[str]) -> None:
    """Isolated child bootstrap: bypass Settings' implicit env-file loading."""
    from revnote_api.config import Settings, get_settings

    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    if args[0] == "_alembic":
        from alembic import command
        from alembic.config import Config

        config = Config(str(REPO_ROOT / "apps" / "api" / "alembic.ini"))
        command.upgrade(config, "head")
        command.current(config, check_heads=True)
        command.check(config)
    else:
        import uvicorn

        from revnote_api.main import create_app

        app = create_app(Settings(_env_file=None))
        run_id = os.environ["WEB_E2E_RUN_ID"]

        @app.middleware("http")
        async def owner_header(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            response = await call_next(request)
            response.headers["X-Revnote-E2E-Owner"] = run_id
            return response

        uvicorn.run(
            app,
            host="127.0.0.1",
            port=int(args[1]),
            access_log=False,
            log_level="critical",
        )


async def database_probe(raw: str, payload: dict[str, object]) -> dict[str, object]:
    """Test-only catalog/row inspection. No SQL strings or bodies accepted over IPC."""
    url = make_url(raw)
    connection = await asyncpg.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database=url.database,
        timeout=10,
    )
    try:
        workspace = uuid.UUID(str(payload["workspaceId"]))
        action = payload.get("action")
        if action == "floor":
            # Construct retention on the disposable test DB, not a production
            # failpoint. Existing events are retained so counts remain inspectable.
            await connection.execute(
                "UPDATE workspaces SET min_retained_seq=GREATEST(1,last_seq) WHERE id=$1",
                workspace,
            )
            return {"ok": True}
        if action != "stats":
            raise LifecycleError("Unknown database probe action.")
        op = uuid.UUID(str(payload["opId"])) if payload.get("opId") else None
        entity = uuid.UUID(str(payload["entityId"])) if payload.get("entityId") else None
        events = await connection.fetchval(
            "SELECT count(*) FROM change_events WHERE workspace_id=$1 "
            "AND ($2::uuid IS NULL OR op_id=$2)",
            workspace,
            op,
        )
        ledger = await connection.fetchval(
            "SELECT count(*) FROM op_ledger WHERE workspace_id=$1 "
            "AND ($2::uuid IS NULL OR op_id=$2)",
            workspace,
            op,
        )
        note = (
            await connection.fetchrow(
                "SELECT rev, body_md, deleted_at FROM notes WHERE workspace_id=$1 AND id=$2",
                workspace,
                entity,
            )
            if entity
            else None
        )
        refresh = await connection.fetchrow(
            "SELECT count(*) AS total, count(*) FILTER (WHERE r.used_at IS NOT NULL) AS used "
            "FROM auth_refresh_tokens r JOIN auth_sessions s ON s.id=r.session_id "
            "JOIN workspaces w ON w.owner_id=s.user_id WHERE w.id=$1",
            workspace,
        )
        return {
            "events": events,
            "ledger": ledger,
            "noteRev": str(note["rev"]) if note else None,
            "noteDeleted": note["deleted_at"] is not None if note else None,
            "bodyHash": hashlib.sha256(note["body_md"].encode()).hexdigest() if note else None,
            "refreshTotal": refresh["total"] if refresh else 0,
            "refreshUsed": refresh["used"] if refresh else 0,
        }
    finally:
        await connection.close()


class TestControl:
    """Runner-local authenticated IPC; never mounted in the production API/Vite."""

    # Only these names may appear in diagnostics. Anything else is recorded as
    # "unknown" so a malformed payload cannot inject text into the CI log.
    ACTIONS = frozenset({"stop", "start", "stats", "floor"})

    def __init__(
        self,
        command: list[str],
        env: dict[str, str],
        port: int,
        owned: list[subprocess.Popen[bytes]],
        job: WindowsJob | None,
        api: subprocess.Popen[bytes],
    ) -> None:
        self.command, self.env, self.port = command, env, port
        self.owned, self.job, self.api = owned, job, api
        self.secret_key = uuid.uuid4().hex
        # Fixed-vocabulary breadcrumb of the last control exchange, so a failed
        # run can distinguish "the browser never started the API again" from
        # "the restart was requested and did not become ready". Never the
        # payload, a credential, a URL or an exception message.
        self.last_ok_action: str | None = None
        self.last_failure: tuple[str, str] | None = None
        control = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass  # No requests, headers or authentication material in logs.

            def do_POST(self) -> None:
                if self.headers.get("X-Revnote-Control") != control.secret_key:
                    control.last_failure = ("unknown", "unauthorized")
                    self.send_error(403)
                    return
                action = "unknown"
                code = "internal"
                try:
                    code = "invalid_length"
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 1 or length > 2048:
                        raise LifecycleError("Invalid control payload length.")
                    code = "invalid_payload"
                    payload = json.loads(self.rfile.read(length))
                    action = control.action_name(payload.get("action"))
                    # Past this point the action name is meaningful, so a failure
                    # is attributed to it rather than to payload parsing.
                    code = "action_failed"
                    if action == "stop":
                        if control.api.poll() is not None:
                            code = "already_stopped"
                            raise LifecycleError("The owned API is already stopped.")
                        stop_owned(control.api)
                        result: dict[str, object] = {"ok": True}
                    elif action == "start":
                        if control.api.poll() is None:
                            code = "already_running"
                            raise LifecycleError("The owned API is already running.")
                        child_env = dict(control.env)
                        if payload.get("snapshotLimit") == 1:
                            child_env["SYNC_SNAPSHOT_MAX_OBJECTS"] = "1"
                        try:
                            reservation = reserve_port(control.port)
                        except LifecycleError:
                            code = "port_unavailable"
                            raise
                        reservation.close()
                        try:
                            control.api = spawn(control.command, child_env, REPO_ROOT, control.job)
                        except LifecycleError:
                            code = "spawn_failed"
                            raise
                        control.owned.append(control.api)
                        try:
                            wait_http(
                                control.api,
                                f"http://127.0.0.1:{control.port}/health",
                                control.env["WEB_E2E_RUN_ID"],
                            )
                        except LifecycleError:
                            # The decisive distinction for a cascade: a restart
                            # that was accepted but never became ready.
                            code = "not_ready_after_start"
                            raise
                        result = {"ok": True}
                    else:
                        code = "unknown_action" if action == "unknown" else "probe_failed"
                        result = asyncio.run(database_probe(control.env["DATABASE_URL"], payload))
                    # The action itself succeeded; only the response write can
                    # still fail, and that is recorded as its own fixed code.
                    control.last_ok_action = action
                    code = "response_write_failed"
                    body = json.dumps(result).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:
                    control.last_failure = (action, code)
                    self.send_error(500, "Owned test control failed; details redacted")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @classmethod
    def action_name(cls, value: object) -> str:
        """Map a payload value onto the fixed vocabulary; never echo the input."""
        return value if isinstance(value, str) and value in cls.ACTIONS else "unknown"

    def describe(self) -> str:
        """One line of fixed-vocabulary control history for a failed run."""
        last_ok = self.last_ok_action or "none"
        if self.last_failure is None:
            last_failure = "none"
        else:
            action, code = self.last_failure
            last_failure = f"{action}/{code}"
        return f"Control: last_ok={last_ok} last_failure={last_failure}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


def process_alive(process: subprocess.Popen[bytes]) -> str:
    """Liveness only: never a command line, environment or exit detail."""
    try:
        return "running" if process.poll() is None else "exited"
    except OSError:
        return "unknown"


def probe_status(url: str) -> str:
    """Return only a numeric HTTP status, or a fixed failure class."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=5) as response:
            return str(response.status)
    except urllib.error.HTTPError as error:
        # A 5xx/4xx from our own service is the useful signal.
        return str(error.code)
    except urllib.error.URLError:
        return "unreachable"
    except (OSError, ValueError):
        return "unreachable"


def print_lifecycle_diagnostics(
    api: subprocess.Popen[bytes] | None,
    web: subprocess.Popen[bytes] | None,
    api_url: str,
    web_url: str,
    control: TestControl | None,
) -> None:
    """Bounded, credential-free state of the owned lifecycle at failure time.

    Distinguishes "the API was restarted and never became ready" from "the
    browser stopped driving the owned API at all" without exposing a single
    child byte of output.
    """
    # The initial API object is replaced whenever a case stops and restarts the
    # owned service, so report the current one as well: "initial exited, current
    # running" is the healthy post-restart state, not a failure.
    current = control.api if control is not None else None
    print(
        "Owned processes: "
        f"api-initial={process_alive(api) if api is not None else 'absent'} "
        f"api-current={process_alive(current) if current is not None else 'absent'} "
        f"vite={process_alive(web) if web is not None else 'absent'}",
        file=sys.stderr,
    )
    print(
        f"Health probes: api={probe_status(f'{api_url}/health')} "
        f"web-proxy={probe_status(f'{web_url}/api/health')}",
        file=sys.stderr,
    )
    if control is not None:
        print(control.describe(), file=sys.stderr)


def safe_assertion(case: dict[str, object]) -> str | None:
    """The whitelisted ``e2e/<spec>.spec.ts:<line>`` of a failing assertion.

    Only the two locating fields are read. A message, stack, snippet or value
    living beside them in the same report object is never touched, so no
    assertion text can reach the log. A location that does not name a real line
    of a real owned spec is dropped rather than printed: a wrong pointer is
    worse evidence than none.
    """
    assertion = case.get("assertion")
    line = case.get("assertionLine")
    if not isinstance(assertion, str) or not re.fullmatch(
        r"e2e/[a-zA-Z0-9_-]+\.spec\.ts", assertion
    ):
        return None
    if type(line) is not int or not 1 <= line <= 1_000_000:
        return None
    e2e_root = (WEB_ROOT / "e2e").resolve()
    target = (WEB_ROOT / assertion).resolve()
    if target.parent != e2e_root or not target.is_file():
        return None
    try:
        if line > len(target.read_text(encoding="utf-8").split("\n")):
            return None
    except OSError:
        return None
    return f"{assertion}:{line}"


def print_safe_browser_failures(report_path: Path, run_id: str, scope: str) -> None:
    """Print only schema-restricted locating metadata, never child error values."""
    labels = {
        "workspace_flow",
        "draft_reload",
        "api_restart_recovery",
        "offline_recovery",
        "lost_response_replay",
        "ack_preserves_later_edit",
        "manual_conflict",
        "remote_delete_recovery",
        "single_writer_takeover",
        "refresh_rotation",
        "refresh_uncertainty",
        "session_identity_isolation",
        "resync_preservation",
        "cursor_integrity",
        "markdown_preservation",
        "mobile_layout",
    }
    try:
        if report_path.stat().st_size > 1_000_000:
            raise LifecycleError("Oversize diagnostic report.")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("runId") != run_id or report.get("scope") != scope:
            raise LifecycleError("Stale diagnostic report.")
        cases = report.get("cases")
        if not isinstance(cases, list):
            raise LifecycleError("Missing diagnostic cases.")
        printed = 0
        e2e_root = (WEB_ROOT / "e2e").resolve()
        for case in cases:
            if not isinstance(case, dict) or case.get("status") == "passed":
                continue
            case_id = case.get("id")
            source = case.get("source")
            line = case.get("line")
            case_labels = case.get("labels")
            if (
                not isinstance(case_id, str)
                or not re.fullmatch(r"[a-f0-9]{64}", case_id)
                or not isinstance(source, str)
                or not re.fullmatch(r"e2e/[a-zA-Z0-9_-]+\.spec\.ts", source)
                or type(line) is not int
                or not 1 <= line <= 1_000_000
                or not isinstance(case_labels, list)
                or len(case_labels) > len(labels)
            ):
                continue
            spec = (WEB_ROOT / source).resolve()
            if spec.parent != e2e_root or not spec.is_file():
                continue
            try:
                # A location that names no real line is worse than none: it sends
                # the reader to unrelated content. Reproduce the line count here
                # instead of trusting the number the browser reported.
                if line > len(spec.read_text(encoding="utf-8").split("\n")):
                    continue
                assertion = safe_assertion(case)
            except OSError:
                continue
            safe_labels = sorted(
                label for label in case_labels if isinstance(label, str) and label in labels
            )
            where = f"assertion={assertion}" if assertion else "assertion=unknown"
            print(
                f"Browser failure: {source}:{line} {where} "
                f"labels={','.join(safe_labels)} case={case_id}",
                file=sys.stderr,
            )
            printed += 1
            if printed >= 25:
                # Bounded output: a mass failure must not flood a CI log.
                print("Further browser failure locations suppressed.", file=sys.stderr)
                break
        if not printed:
            print(
                "No case-level location available; browser failed before located results.",
                file=sys.stderr,
            )
    except (OSError, ValueError, LifecycleError, TypeError, AttributeError):
        print("Restricted browser diagnostics unavailable for this invocation.", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    class Parser(argparse.ArgumentParser):
        def error(self, message: str) -> NoReturn:
            # argparse would echo an unknown argument back verbatim, so a
            # caller-supplied value could carry a credential or note text into
            # the log. Fail with one fixed, bounded message instead.
            del message
            raise LifecycleError(
                "Invalid runner arguments; usage is --database-url/--api-port/--web-port "
                "plus optional file selectors and --grep/--grep-invert/--timeout only."
            )

    parser = Parser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("WEB_E2E_DATABASE_URL", ""))
    parser.add_argument("--api-port", type=int, default=0)
    parser.add_argument("--web-port", type=int, default=0)
    parser.add_argument("playwright_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    database_url = validate_database_url(args.database_url)
    browser_args = args.playwright_args
    while browser_args[:1] == ["--"]:
        browser_args = browser_args[1:]
    # Never accept a custom config/reporter/output/project, test-update, list-only,
    # watch/UI mode, retry budget or other gate bypass through forwarded options.
    allowed = {"--grep", "--grep-invert", "--timeout"}
    index = 0
    while index < len(browser_args):
        item = browser_args[index]
        if item.startswith("-"):
            if item not in allowed or index + 1 == len(browser_args):
                raise LifecycleError(
                    "Only file selectors and --grep/--grep-invert/--timeout are forwarded."
                )
            index += 2
        else:
            if not re.fullmatch(r"[a-zA-Z0-9_./:-]+", item):
                raise LifecycleError("Invalid browser test file selector.")
            index += 1
    scope = "focused" if browser_args else "full"
    pnpm = shutil.which("pnpm.cmd" if os.name == "nt" else "pnpm")
    if pnpm is None:
        raise LifecycleError("pnpm is required for the owned browser lifecycle.")
    # Existing Vite configuration explicitly calls loadEnv at the repo root, and
    # the API reads ``apps/api/.env`` implicitly. Refuse applicable env files
    # without opening them rather than letting either child read development
    # credentials or override the explicit test target.
    for relative in (
        ".env",
        ".env.local",
        ".env.e2e",
        ".env.e2e.local",
        "apps/api/.env",
        "apps/api/.env.local",
        "apps/web/.env",
        "apps/web/.env.local",
    ):
        if (REPO_ROOT / relative).exists():
            raise LifecycleError(
                "Remove applicable development env files from this test workspace."
            )
    asyncio.run(require_empty_database(database_url))
    run_id = uuid.uuid4().hex
    env = child_environment(database_url, run_id)
    owned: list[subprocess.Popen[bytes]] = []
    reservations: list[socket.socket] = []
    job = WindowsJob() if os.name == "nt" else None
    control: TestControl | None = None
    try:
        api_socket = reserve_port(args.api_port)
        reservations.append(api_socket)
        web_socket = reserve_port(args.web_port)
        reservations.append(web_socket)
        api_port = api_socket.getsockname()[1]
        web_port = web_socket.getsockname()[1]
        api_url = f"http://127.0.0.1:{api_port}"
        web_url = f"http://127.0.0.1:{web_port}"
        env.update(
            WEB_E2E_BASE_URL=web_url,
            WEB_E2E_API_URL=api_url,
            WEB_TEST_REPORT_SCOPE=scope,
            API_PROXY_TARGET=api_url,
        )
        command = [sys.executable, str(Path(__file__).resolve())]
        run_checked([*command, "_alembic"], env, REPO_ROOT, owned, job)
        api_socket.close()
        api = spawn([*command, "_api", str(api_port)], env, REPO_ROOT, job)
        owned.append(api)
        wait_http(api, f"{api_url}/health", run_id)
        control = TestControl([*command, "_api", str(api_port)], env, api_port, owned, job, api)
        web_socket.close()
        web = spawn(
            [
                pnpm,
                "exec",
                "vite",
                "--host",
                "127.0.0.1",
                "--port",
                str(web_port),
                "--strictPort",
                "--mode",
                "e2e",
            ],
            env,
            WEB_ROOT,
            job,
        )
        owned.append(web)
        # Proxy readiness proves this Vite reaches the owned API, not an
        # unrelated process that happened to bind the selected port.
        wait_http(web, f"{web_url}/api/health", run_id)
        report_dir = WEB_ROOT / "reports" / scope
        report_dir.mkdir(parents=True, exist_ok=True)
        for suffix in ("json", "xml"):
            (report_dir / f"playwright.{suffix}").unlink(missing_ok=True)
        browser_env = {
            **env,
            "WEB_E2E_CONTROL_URL": control.url,
            "WEB_E2E_CONTROL_KEY": control.secret_key,
        }
        # The browser process never needs the database DSN. Keep credentials out
        # of the test runner process that Chromium-driven specs control.
        for key in ("DATABASE_URL", "WEB_E2E_DATABASE_URL"):
            browser_env.pop(key, None)
        try:
            run_checked(
                [pnpm, "exec", "playwright", "test", *browser_args],
                browser_env,
                WEB_ROOT,
                owned,
                job,
                # Whole-suite budget: 16 cases at a 180 s ceiling plus setup.
                timeout=3_600 if scope == "full" else 1_800,
            )
        except LifecycleError:
            # Browser exited nonzero: report what the owned lifecycle looked like
            # at that moment before the located case metadata.
            print_lifecycle_diagnostics(api, web, api_url, web_url, control)
            print_safe_browser_failures(report_dir / "playwright.json", run_id, scope)
            raise
        report = json.loads((report_dir / "playwright.json").read_text(encoding="utf-8"))
        if report.get("runId") != run_id or report.get("scope") != scope:
            raise LifecycleError("The browser report does not belong to this invocation.")
        try:
            run_checked(
                [
                    pnpm,
                    "exec",
                    "tsx",
                    "scripts/check-test-reports.ts",
                    "--scope",
                    scope,
                    "--runner",
                    "playwright",
                ],
                env,
                WEB_ROOT,
                owned,
                job,
            )
        except LifecycleError:
            # Browser exited zero but the evidence was rejected (skip, retry,
            # failure, missing label, or a JSON/JUnit disagreement). The located
            # metadata is printed before the fixed redacted error is raised.
            print_safe_browser_failures(report_dir / "playwright.json", run_id, scope)
            raise
        print(f"Web E2E {scope}: passed; browser report guarded; child output redacted.")
        return 0
    finally:
        # Cleanup must never replace the failure that caused it: a raise inside
        # `finally` would hide the original evidence (and skip the safe output).
        cleanup_failed = False
        try:
            if control is not None:
                control.close()
        except (OSError, RuntimeError):
            cleanup_failed = True
        for reservation in reservations:
            try:
                reservation.close()
            except OSError:
                cleanup_failed = True
        if job is not None:
            job.close()
        for process in reversed(owned):
            try:
                stop_owned(process)
            except (OSError, subprocess.SubprocessError):
                cleanup_failed = True
        if cleanup_failed:
            print(
                "An owned process or socket could not be cleaned up; manual review required.",
                file=sys.stderr,
            )
        print("Owned API/Vite/browser groups stopped; external PostgreSQL remains caller-owned.")
        # Fail on incomplete cleanup only when nothing else already failed:
        # raising during exception propagation would replace the real cause.
        if cleanup_failed and sys.exc_info()[0] is None:
            raise LifecycleError("Owned-process cleanup was incomplete.")


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] in {"_api", "_alembic"}:
            internal_entrypoint(sys.argv[1:])
        else:
            raise SystemExit(main())
    except KeyboardInterrupt:
        print("Web E2E interrupted; owned-process cleanup executed.", file=sys.stderr)
        raise SystemExit(130) from None
    except LifecycleError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print(
            "Web E2E failed; diagnostic details redacted to protect credentials and note bodies.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
