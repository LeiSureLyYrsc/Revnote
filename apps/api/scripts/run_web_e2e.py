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
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import asyncpg
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from starlette.requests import Request
from starlette.responses import Response

REPO_ROOT = Path(__file__).resolve().parents[3]
WEB_ROOT = REPO_ROOT / "apps" / "web"


class LifecycleError(RuntimeError):
    """A safe, fixed-message lifecycle failure (never include child output)."""


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

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
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
        others = await connection.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname=current_database() AND pid<>pg_backend_pid()"
        )
        if objects or others:
            raise LifecycleError(
                "The web test database must be fresh, empty and exclusively owned."
            )
    finally:
        await connection.close()


def reserve_port(requested: int) -> socket.socket:
    reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        reservation.bind(("127.0.0.1", requested))
        reservation.listen(1)
    except OSError:
        reservation.close()
        raise LifecycleError("A requested HTTP port is already occupied; refusing reuse.") from None
    return reservation


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
) -> None:
    process = spawn(command, env, cwd, job)
    owned.append(process)
    try:
        code = process.wait(timeout=600)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
    # Existing Vite configuration explicitly calls loadEnv at the repo root.
    # Refuse applicable env files without opening them rather than letting that
    # call load development credentials or override the explicit API target.
    for name in (".env", ".env.local", ".env.e2e", ".env.e2e.local"):
        if (REPO_ROOT / name).exists():
            raise LifecycleError(
                "Remove applicable development env files from this test workspace."
            )
    asyncio.run(require_empty_database(database_url))
    run_id = uuid.uuid4().hex
    env = child_environment(database_url, run_id)
    owned: list[subprocess.Popen[bytes]] = []
    reservations: list[socket.socket] = []
    job = WindowsJob() if os.name == "nt" else None
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
        run_checked([pnpm, "exec", "playwright", "test", *browser_args], env, WEB_ROOT, owned, job)
        report = json.loads((report_dir / "playwright.json").read_text(encoding="utf-8"))
        if report.get("runId") != run_id or report.get("scope") != scope:
            raise LifecycleError("The browser report does not belong to this invocation.")
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
        print(f"Web E2E {scope}: passed; browser report guarded; child output redacted.")
        return 0
    finally:
        for reservation in reservations:
            reservation.close()
        cleanup_failed = False
        if job is not None:
            job.close()
        for process in reversed(owned):
            try:
                stop_owned(process)
            except (OSError, subprocess.SubprocessError):
                cleanup_failed = True
        if cleanup_failed:
            raise LifecycleError(
                "An owned process could not be cleaned up; manual review required."
            )
        print("Owned API/Vite/browser groups stopped; external PostgreSQL remains caller-owned.")


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
