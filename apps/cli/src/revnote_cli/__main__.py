"""``revnote`` entry point: argument grammar, dispatch and exit codes.

The grammar is frozen in ``docs/phase8-reference-cli.md``. Global options may be
given before or after the command (each subcommand carries the same common
parser, with ``SUPPRESS`` defaults so an earlier value is never overwritten by a
later parser's default).

Exit codes are the contract, not a detail: ``0`` success, ``2`` usage, ``3``
authentication, ``4`` conflict, ``5`` API/network, ``6`` not found. ``--help``
and the documentation table are generated from the same constants in
:mod:`revnote_cli.errors`.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import sys
from collections.abc import Sequence

from revnote_cli import __version__
from revnote_cli.client import RevnoteClient
from revnote_cli.commands import CommandResult
from revnote_cli.commands import attachment as attachment_commands
from revnote_cli.commands import auth as auth_commands
from revnote_cli.commands import read as read_commands
from revnote_cli.commands import sync as sync_commands
from revnote_cli.config import (
    API_BASE_ENV,
    DEFAULT_API_BASE,
    CredentialStore,
    StoredCredentials,
    resolve_config_dir,
)
from revnote_cli.errors import (
    EXIT_API,
    EXIT_AUTH,
    EXIT_CONFLICT,
    EXIT_INTERNAL,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    CliError,
)
from revnote_cli.output import emit_error, emit_json, emit_text

__all__ = ["build_parser", "main"]

EXIT_TABLE = (
    "exit codes: 0 success, 2 usage, 3 authentication, 4 conflict, 5 API/network, 6 not found"
)

_EPILOG = f"""\
{EXIT_TABLE}

examples:
  revnote login --email you@example.com
  revnote tree --workspace <uuid>
  revnote sync snapshot --page-size 200
  revnote sync pull --since 41
  revnote sync push note create --title "Hello" --body-text "World"
  revnote sync push note update <note-uuid> --base-rev 1 --title "Hello"
  revnote --json capabilities
"""


def _common() -> argparse.ArgumentParser:
    """Options accepted by every command, at every depth."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--api-base",
        default=argparse.SUPPRESS,
        metavar="URL",
        help=f"API root (env REVNOTE_API_BASE; default {DEFAULT_API_BASE}).",
    )
    parser.add_argument(
        "--config-dir",
        default=argparse.SUPPRESS,
        metavar="DIR",
        help="Credential directory (env REVNOTE_CONFIG_DIR; platform default otherwise).",
    )
    parser.add_argument(
        "--workspace",
        default=argparse.SUPPRESS,
        metavar="ID",
        help="Workspace id; defaults to the saved one, or the only membership.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print one JSON document on stdout (errors on stderr).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=argparse.SUPPRESS,
        metavar="SECONDS",
        help="HTTP timeout in seconds (default 30).",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    """The whole command grammar (one parser, no hidden subcommands)."""
    common = _common()
    parser = argparse.ArgumentParser(
        prog="revnote",
        description="Revnote reference CLI - native /api/v1 client.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"revnote {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", parents=[common], help="Exchange credentials for a session.")
    login.add_argument("--email", required=True)
    login.add_argument(
        "--password",
        help="Password; omit to be prompted, or use --password-stdin for scripts.",
    )
    login.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the password from the first stdin line (nothing is echoed).",
    )
    login.add_argument("--device-name", default="revnote-cli")
    login.set_defaults(handler=_login)

    logout = sub.add_parser(
        "logout", parents=[common], help="Revoke the session and delete tokens."
    )
    logout.set_defaults(handler=auth_commands.logout)

    refresh = sub.add_parser("refresh", parents=[common], help="Rotate the refresh token.")
    refresh.set_defaults(handler=auth_commands.refresh)

    me = sub.add_parser(
        "me", parents=[common], aliases=["whoami"], help="Show the caller and its workspaces."
    )
    me.set_defaults(handler=auth_commands.me)

    tree = sub.add_parser("tree", parents=[common], help="Active folders and note metadata.")
    tree.set_defaults(handler=read_commands.tree)

    note = sub.add_parser("note", parents=[common], help="Read one note.")
    note_sub = note.add_subparsers(dest="note_action", required=True)
    note_get = note_sub.add_parser("get", parents=[common], help="One note with its Markdown body.")
    note_get.add_argument("note_id", metavar="NOTE_ID")
    note_get.set_defaults(handler=read_commands.note_get)

    capabilities = sub.add_parser(
        "capabilities", parents=[common], help="Deployment feature detection."
    )
    capabilities.set_defaults(handler=read_commands.capabilities)

    sync = sub.add_parser("sync", parents=[common], help="Snapshot, pull and push.")
    sync_sub = sync.add_subparsers(dest="sync_command", required=True)

    snapshot = sync_sub.add_parser(
        "snapshot", parents=[common], help="Bootstrap the active set (paged when offered)."
    )
    snapshot.add_argument(
        "--page-size",
        type=int,
        default=None,
        help="Explicit page size; selects paging even without the capability.",
    )
    snapshot.set_defaults(handler=sync_commands.snapshot)

    pull = sync_sub.add_parser("pull", parents=[common], help="Read events after --since.")
    pull.add_argument("--since", default="0", metavar="SEQ", help="Exclusive cursor (default 0).")
    pull.add_argument("--limit", type=int, default=None, help="Server page size.")
    pull.add_argument(
        "--once", action="store_true", help="Return the first page instead of draining the cursor."
    )
    pull.set_defaults(handler=sync_commands.pull)

    push = sync_sub.add_parser(
        "push", parents=[common], help="Apply a batch, or create/update one note."
    )
    push.add_argument("--ops-file", metavar="FILE", help='JSON array of ops, or {"ops": [...]}.')
    push.set_defaults(handler=sync_commands.push)
    push_sub = push.add_subparsers(dest="push_command")
    push_note = push_sub.add_parser("note", parents=[common], help="Note convenience commands.")
    push_note_sub = push_note.add_subparsers(dest="note_command", required=True)

    push_create = push_note_sub.add_parser("create", parents=[common], help="Create one note.")
    push_create.add_argument("--title", required=True)
    push_create.add_argument("--body-file", metavar="FILE")
    push_create.add_argument("--body-text", metavar="TEXT")
    push_create.add_argument("--folder-id", metavar="ID")
    push_create.add_argument(
        "--entity-id", metavar="ID", help="Client-chosen note id (idempotency)."
    )
    push_create.set_defaults(handler=sync_commands.push)

    push_update = push_note_sub.add_parser("update", parents=[common], help="Update one note.")
    push_update.add_argument("note_id", metavar="NOTE_ID")
    push_update.add_argument("--base-rev", required=True, metavar="REV")
    push_update.add_argument("--title", required=True)
    push_update.add_argument("--body-file", metavar="FILE")
    push_update.add_argument("--body-text", metavar="TEXT")
    push_update.set_defaults(handler=sync_commands.push)

    attachment = sub.add_parser("attachment", parents=[common], help="Upload, download, inspect.")
    attachment_sub = attachment.add_subparsers(dest="attachment_command", required=True)

    upload = attachment_sub.add_parser("upload", parents=[common], help="Create and upload a file.")
    upload.add_argument("path", metavar="PATH")
    upload.add_argument("--mime", help="Declared media type (default: guessed from the name).")
    upload.add_argument("--name", help="Display name sent with the create request.")
    upload.set_defaults(handler=attachment_commands.upload)

    download = attachment_sub.add_parser(
        "download", parents=[common], help="Stream one attachment to a file."
    )
    download.add_argument("attachment_id", metavar="ATTACHMENT_ID")
    download.add_argument("--output", required=True, metavar="PATH")
    download.set_defaults(handler=attachment_commands.download)

    info = attachment_sub.add_parser(
        "info", parents=[common], help="Metadata of one ready attachment."
    )
    info.add_argument("attachment_id", metavar="ATTACHMENT_ID")
    info.set_defaults(handler=attachment_commands.info)

    return parser


def _login(client: RevnoteClient, args: argparse.Namespace) -> CommandResult:
    """Resolve the password through exactly one channel, then call the command."""
    if args.password_stdin:
        if args.password is not None:
            raise CliError(
                "--password and --password-stdin are mutually exclusive.",
                code="usage",
                exit_code=EXIT_USAGE,
            )
        password = sys.stdin.readline().rstrip("\r\n")
    elif args.password is not None:
        password = args.password
    else:
        if not sys.stdin.isatty():
            raise CliError(
                "No terminal for a password prompt; use --password-stdin.",
                code="usage",
                exit_code=EXIT_USAGE,
            )
        password = getpass.getpass("Password: ")
    if not password:
        raise CliError("The password must not be empty.", code="usage", exit_code=EXIT_USAGE)
    args.password = password
    return auth_commands.login(client, args)


def _fill_defaults(args: argparse.Namespace) -> None:
    """Defaults for the SUPPRESS'd common options (whichever parser saw them)."""
    defaults = {
        "api_base": None,
        "config_dir": None,
        "workspace": None,
        "json": False,
        "timeout": 30.0,
    }
    for name, value in defaults.items():
        if not hasattr(args, name):
            setattr(args, name, value)


def _build_client(args: argparse.Namespace) -> RevnoteClient:
    """Credentials from the resolved directory; the API base follows flag > env > saved."""
    if args.timeout <= 0:
        raise CliError("--timeout must be positive.", code="usage", exit_code=EXIT_USAGE)
    store = CredentialStore(resolve_config_dir(args.config_dir))
    credentials = store.load() or StoredCredentials()
    from_env = os.environ.get(API_BASE_ENV, "").strip()
    base = args.api_base or from_env or credentials.api_base or DEFAULT_API_BASE
    credentials.api_base = base
    return RevnoteClient(credentials, store, timeout=args.timeout)


def _prepare_streams() -> None:
    """Never fail on a console encoding when a title carries non-ASCII text."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(OSError, ValueError):
                reconfigure(errors="backslashreplace")


def main(
    argv: Sequence[str] | None = None,
    *,
    client: RevnoteClient | None = None,
) -> int:
    """Run one command and return its exit code (the console script exits with it)."""
    _prepare_streams()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:  # --help / --version / a usage error
        code = exit_.code
        if code is None:
            return EXIT_OK
        return code if isinstance(code, int) else EXIT_USAGE

    _fill_defaults(args)
    json_mode = bool(args.json)
    active = client
    try:
        if active is None:
            active = _build_client(args)
        result = args.handler(active, args)
        if json_mode:
            emit_json(result.payload)
        else:
            emit_text(result.lines)
        if result.exit_code != EXIT_OK:
            emit_error(
                CliError(
                    result.error_message or "The command reported a failed operation.",
                    code=result.error_code,
                    exit_code=result.exit_code,
                ),
                json_mode=json_mode,
            )
        return result.exit_code
    except CliError as error:
        emit_error(error, json_mode=json_mode)
        return error.exit_code
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 130
    except Exception as error:
        emit_error(
            CliError(
                f"Unexpected error ({type(error).__name__}).",
                code="internal",
                exit_code=EXIT_INTERNAL,
            ),
            json_mode=json_mode,
        )
        return EXIT_INTERNAL
    finally:
        if client is None and active is not None:
            active.close()


#: Re-exported for the documentation's exit table and for tests.
EXIT_CODES = {
    "ok": EXIT_OK,
    "usage": EXIT_USAGE,
    "auth": EXIT_AUTH,
    "conflict": EXIT_CONFLICT,
    "api": EXIT_API,
    "not_found": EXIT_NOT_FOUND,
}


if __name__ == "__main__":  # pragma: no cover - module execution convenience
    raise SystemExit(main())
