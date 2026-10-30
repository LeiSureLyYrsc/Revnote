"""Delete expired ``pending`` attachments and their objects. Never touches ``ready``.

Usage::

    python -m revnote_api.tools.attachments_gc --dry-run
    python -m revnote_api.tools.attachments_gc

What it collects
----------------
A row is eligible when **both** hold, and both are re-checked in the statement that
deletes it (never in a read-then-write pair):

* ``state = 'pending'`` - an upload that was created and never completed;
* ``created_at < now() - ATTACHMENT_PENDING_GRACE_S`` - and the grace window comes
  from the same settings the API reads, so the two cannot disagree about which rows
  are already refused to clients.

``ready`` rows are invisible to the scan by construction, so a stored attachment is
never collected even if the process runs on every host (D8). ``deleted`` rows are
left alone too: they are the audit trail of what the GC already removed.

Order of work, and why
----------------------
Row first, then object. If the process dies in between, the result is an orphan
*object* (invisible, costs storage, collectable later) rather than a row whose
object is gone (a user-visible 404 on something the database says is ``ready``).
A concurrent upload is safe for the same reason: the loser's CAS matches no row, and
it then deletes its own key.

Output is counts only - never a key, a file name or an id. This runs from a
terminal, cron and CI logs, and an attachment name is user content.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import Final

from revnote_api.api.routes.attachments import build_store
from revnote_api.config import Settings, get_settings
from revnote_api.db.session import get_sessionmaker
from revnote_api.services.attachments import collect_expired

__all__ = ["main", "run"]

#: Exit code for "the run completed", whether or not it removed anything.
EXIT_OK: Final[int] = 0
#: Exit code for "at least one row or object could not be handled". A cron job can
#: alert on this without parsing the output.
EXIT_PARTIAL: Final[int] = 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m revnote_api.tools.attachments_gc",
        description=(
            "Delete attachments that were created but never uploaded, and their "
            "objects. Rows in state 'ready' are never touched."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many rows would be collected, and delete nothing.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Collect at most N rows in this run (default: no limit).",
    )
    parser.add_argument(
        "--grace-seconds",
        type=int,
        default=None,
        metavar="S",
        help=(
            "Override ATTACHMENT_PENDING_GRACE_S for this run. Useful to collect a "
            "backlog; never smaller than the API's own window unless you mean it."
        ),
    )
    return parser


async def run(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    grace_seconds: int | None = None,
    settings: Settings | None = None,
) -> int:
    """Perform one collection pass and return the process exit code.

    ``settings`` is injectable so a test can point the tool at its own app
    configuration without touching a global; production passes ``None`` and gets the
    process-wide settings.
    """
    resolved = settings or get_settings()
    grace = (
        int(grace_seconds)
        if grace_seconds is not None
        else int(resolved.attachment_pending_grace_s)
    )
    store = build_store(resolved)
    report = await collect_expired(
        get_sessionmaker(resolved),
        store,
        grace_s=grace,
        dry_run=dry_run,
        limit=limit,
    )

    prefix = "dry-run: would collect" if dry_run else "collected"
    print(
        f"attachments_gc scanned={report.scanned} "
        f"{'rows_matched' if dry_run else 'rows_deleted'}={report.rows_deleted} "
        f"{'objects_matched' if dry_run else 'objects_deleted'}={report.objects_deleted} "
        f"failed={report.failed} ({prefix})"
    )
    return EXIT_OK if report.failed == 0 else EXIT_PARTIAL


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: parse arguments, then run one pass."""
    arguments = _parser().parse_args(argv)
    if arguments.limit is not None and arguments.limit < 1:
        _parser().error("--limit must be a positive integer")
    try:
        return asyncio.run(
            run(
                dry_run=arguments.dry_run,
                limit=arguments.limit,
                grace_seconds=arguments.grace_seconds,
            )
        )
    except KeyboardInterrupt:  # pragma: no cover - operator interrupt
        print("attachments_gc interrupted", file=sys.stderr)
        return EXIT_PARTIAL


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
