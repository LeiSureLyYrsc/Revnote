"""``GET .../sync/pull`` and ``GET .../sync/snapshot`` - the bootstrap reads.

Both endpoints are thin: the consistency work lives in
:mod:`revnote_api.services.sync_read` (one statement for pull, one
``REPEATABLE READ, READ ONLY`` transaction for snapshot), and this module only

* parses the wire form (``since`` as a canonical decimal string, ``limit`` bounded
  by configuration),
* renders the service rows into the published models,
* and enforces the two **byte** budgets on the actually serialized JSON.

Why the budget check lives here
-------------------------------
A byte budget is a property of the response body, and escaping is a property of
the encoder: ``"\n"`` is two bytes on the wire, ``"\\u0001"`` is six, and a
multi-byte code point is up to four. Measuring the Python ``str`` would understate
all three. So the response is serialized exactly as it will be sent
(:func:`~revnote_api.api.schemas.sync_pull.dump_body`) and the number that is
compared against the budget is that byte length.

* **Pull** shortens the *contiguous* prefix of events. History is never compacted
  and no event is ever skipped: dropping a middle event would move ``next_cursor``
  past unseen data. The object list is recomputed from the rows already fetched
  (no second query) so it always describes exactly the events that remain. If not
  even the first event can be shipped, the page is refused with
  ``413 payload_too_large`` - an empty page marked ``has_more`` would trap the
  client in a loop.
* **Snapshot** is all-or-nothing (D15): oversize is ``409 snapshot_too_large`` and
  never a partial list.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response

from revnote_api.api.dependencies import AuthContext, require_workspace_member
from revnote_api.api.errors import ApiError
from revnote_api.api.schemas.common import CURSOR_ZERO, DecimalString, render_decimal
from revnote_api.api.schemas.sync_pull import (
    FolderObject,
    NoteObject,
    PullEvent,
    PullResponse,
    SnapshotResponse,
    dump_body,
    dump_size,
)
from revnote_api.config import Settings
from revnote_api.db.session import get_sessionmaker
from revnote_api.services.sync_read import (
    PullPage,
    ReadEvent,
    ReadFolder,
    ReadObject,
    SyncReadError,
    SyncReadService,
)

__all__ = ["ReadServiceDep", "get_read_service", "router"]

router = APIRouter(tags=["sync"])

#: How the query parameter is published; the validator (not this description) is
#: what enforces the canonical form.
_SINCE_DESCRIPTION: Final[str] = (
    "Exclusive lower bound on ``seq``; canonical unsigned decimal string "
    "(default '0', max 9223372036854775807)."
)

#: Query parameters that would imply a cursor or pagination. A snapshot has neither
#: (D15), and silently ignoring one would answer "page 2" or "since X" with page 1.
_PAGINATION_PARAMETERS: Final[frozenset[str]] = frozenset(
    {"cursor", "limit", "offset", "page", "since"}
)

#: Widest canonical decimal that can fit a PostgreSQL ``BIGINT``
#: (``9223372036854775807`` = 19 digits). Anything longer is out of range *and* must
#: be refused before ``int()`` sees it: Python's own int-from-string limit is a
#: global interpreter setting (``sys.set_int_max_str_digits``), so relying on it
#: would make the error depend on process configuration - and converting a
#: megabyte of digits is wasted work even when it succeeds.
MAX_BIGINT_DIGITS: Final[int] = 19


def _parse_cursor(value: str) -> int:
    """Decode ``since`` after a length bound, refusing anything that cannot be a BIGINT.

    The pattern validator already guarantees canonical ASCII digits, so this only
    rejects what is *too long* to be a sequence number. A cursor is compared against
    ``last_seq``, which is a ``BIGINT``: a longer number cannot be a valid position
    in this workspace, so ``invalid_request`` is the honest answer and no arithmetic
    on attacker-sized input happens.
    """
    if len(value) > MAX_BIGINT_DIGITS:
        raise ApiError(
            "invalid_request",
            details={"fields": [{"path": ["query", "since"], "reason": "out_of_range"}]},
        )
    return int(value)


def get_read_service(request: Request) -> SyncReadService:
    """The read service for this request, built from the app's own settings.

    A FastAPI dependency rather than a module-level helper: tests replace it with
    ``app.dependency_overrides`` (the framework's own seam), and the service stays
    stateless - it stores one session factory, so constructing it per request costs
    nothing and no module global can pin the first app's engine.
    """
    settings: Settings = request.app.state.settings
    return SyncReadService(get_sessionmaker(settings))


#: The annotated, injectable form of :func:`get_read_service`.
ReadServiceDep = Annotated[SyncReadService, Depends(get_read_service)]


def _translate(error: SyncReadError) -> ApiError:
    """Carry the service's safe code and scalar details onto the wire."""
    details = {
        key: value
        for key, value in error.details.items()
        if isinstance(value, (str, int, float, bool))
    }
    return ApiError(error.code, details=details)


def _event_model(event: ReadEvent) -> PullEvent:
    return PullEvent(
        seq=render_decimal(event.seq),
        entity_type=event.entity_type,
        entity_id=event.entity_id,
        op=event.op,
        rev=render_decimal(event.rev),
        op_id=event.op_id,
        created_at=event.created_at,
    )


def _object_model(obj: ReadObject) -> FolderObject | NoteObject:
    """Render one current object, tombstone flag included.

    ``deleted`` is derived from ``deleted_at`` rather than stored separately: two
    sources of truth for one fact is how a client ends up with an object that is
    both live and deleted. The object always reports its *real* current
    ``rev``/``updated_seq``, which may be newer than the event that introduced it.
    """
    if isinstance(obj, ReadFolder):
        return FolderObject(
            id=obj.id,
            workspace_id=obj.workspace_id,
            parent_id=obj.parent_id,
            name=obj.name,
            rev=render_decimal(obj.rev),
            updated_seq=render_decimal(obj.updated_seq),
            deleted=obj.deleted_at is not None,
            deleted_at=obj.deleted_at,
            created_at=obj.created_at,
            updated_at=obj.updated_at,
        )
    return NoteObject(
        id=obj.id,
        workspace_id=obj.workspace_id,
        folder_id=obj.folder_id,
        title=obj.title,
        body_md=obj.body_md,
        rev=render_decimal(obj.rev),
        updated_seq=render_decimal(obj.updated_seq),
        deleted=obj.deleted_at is not None,
        deleted_at=obj.deleted_at,
        created_at=obj.created_at,
        updated_at=obj.updated_at,
    )


#: Response headers for every read that can carry note text: an authorised body is
#: not a cacheable public resource.
_PRIVATE_READ: Final[dict[str, str]] = {"Cache-Control": "no-store"}


def _fit_to_budget(page: PullPage, max_bytes: int) -> PullResponse:
    """Render ``page``, shortening its contiguous event prefix until it fits.

    Objects are re-derived from the *already fetched* rows for every candidate
    prefix - no extra query, and no object may describe an event the client did not
    receive. ``next_cursor``/``has_more`` follow the shortened page: the cursor is
    the last event actually shipped, and any dropped event means "more exists".

    The serialized size is monotone in the prefix length - a longer prefix adds an
    event and can only add an object, never remove one - so a binary search finds
    the longest prefix that fits in ``O(log n)`` measurements instead of shrinking
    one event at a time.

    Raises ``payload_too_large`` when not even one event (with its objects) fits:
    skipping that event would silently lose history, and returning an empty page
    with ``has_more`` would make the client poll the same cursor forever.
    """

    def render(count: int) -> PullResponse:
        events = page.events[:count]
        wanted = {event.key for event in events}
        return PullResponse(
            events=[_event_model(event) for event in events],
            objects=[_object_model(obj) for obj in page.objects if obj.key in wanted],
            next_cursor=render_decimal(events[-1].seq if events else page.since),
            has_more=count < len(page.events) or page.has_more,
            last_seq=render_decimal(page.last_seq),
            min_retained_seq=render_decimal(page.min_retained_seq),
        )

    full = render(len(page.events))
    if not page.events:
        # Nothing to shorten: an empty page is already the smallest possible body.
        return full
    if dump_size(full) <= max_bytes:
        return full

    low, high = 0, len(page.events) - 1  # ``high`` is known not to fit (>= 1 event)
    while low < high:
        middle = (low + high + 1) // 2
        if dump_size(render(middle)) <= max_bytes:
            low = middle
        else:
            high = middle - 1

    if low == 0:
        raise ApiError("payload_too_large")
    return render(low)


@router.get(
    "/workspaces/{workspace_id}/sync/pull",
    response_model=PullResponse,
    summary="One consistent page of events plus their current objects",
    responses={
        200: {"description": "Complete contiguous event page and deduplicated objects."},
        400: {"description": "Malformed cursor, or a cursor ahead of this workspace."},
        410: {"description": "The cursor is older than the retained history; resync."},
        413: {"description": "A single event and its object exceed the page budget."},
    },
)
async def pull(
    workspace_id: uuid.UUID,
    request: Request,
    context: Annotated[AuthContext, Depends(require_workspace_member)],
    service: ReadServiceDep,
    since: Annotated[DecimalString, Query(description=_SINCE_DESCRIPTION)] = CURSOR_ZERO,
    limit: Annotated[int | None, Query(ge=1, description="Page size.")] = None,
) -> Response:
    """Read ``(since, ...]`` in one statement; the cursor is exclusive (D13)."""
    del context  # authorisation side effect only; the workspace id is the scope

    cursor = _parse_cursor(since)

    settings: Settings = request.app.state.settings
    page_limit = settings.sync_pull_default_limit if limit is None else limit
    if page_limit > settings.sync_pull_max_limit:
        raise ApiError(
            "invalid_request",
            details={"fields": [{"path": ["query", "limit"], "reason": "too_large"}]},
        )

    try:
        page = await service.pull(
            workspace_id,
            since=cursor,
            limit=page_limit,
            max_bytes=settings.sync_pull_max_bytes,
        )
    except SyncReadError as error:
        raise _translate(error) from None

    payload = _fit_to_budget(page, settings.sync_pull_max_bytes)
    return Response(
        content=dump_body(payload),
        media_type="application/json",
        headers=_PRIVATE_READ,
    )


@router.get(
    "/workspaces/{workspace_id}/sync/snapshot",
    response_model=SnapshotResponse,
    summary="Complete bootstrap from one REPEATABLE READ snapshot",
    responses={
        200: {"description": "Every active object at as_of_seq; continue with pull."},
        409: {"description": "The workspace is too large for a full snapshot."},
    },
)
async def snapshot(
    workspace_id: uuid.UUID,
    request: Request,
    context: Annotated[AuthContext, Depends(require_workspace_member)],
    service: ReadServiceDep,
) -> Response:
    """All active objects and both watermarks from one read-only RR transaction.

    ``cursor``/``limit`` are deliberately **not** declared: pagination of a
    snapshot is not part of v1 (D15), and an unknown query parameter would
    otherwise be ignored - a client that asked for page 2 must be told the
    contract has no pages rather than silently receive page 1 again.
    """
    del context

    settings: Settings = request.app.state.settings
    supplied = {name for name in request.query_params if name in _PAGINATION_PARAMETERS}
    if supplied:
        raise ApiError(
            "invalid_request",
            details={
                "fields": [
                    {"path": ["query", name], "reason": "not_supported"}
                    for name in sorted(supplied)
                ]
            },
        )

    try:
        view = await service.snapshot(
            workspace_id,
            max_objects=settings.sync_snapshot_max_objects,
            max_bytes=settings.sync_snapshot_max_bytes,
        )
    except SyncReadError as error:
        raise _translate(error) from None

    payload = SnapshotResponse(
        as_of_seq=render_decimal(view.as_of_seq),
        min_retained_seq=render_decimal(view.min_retained_seq),
        objects=[_object_model(obj) for obj in view.objects],
    )
    # Serialized once, and that one buffer is both measured and sent: the SQL guard
    # counted *content* bytes, escaping can still inflate them, and the budget is
    # about bytes on the wire. Same answer either way - the whole snapshot is
    # refused, never truncated (D15).
    body = dump_body(payload)
    if len(body) > settings.sync_snapshot_max_bytes:
        raise ApiError("snapshot_too_large")

    return Response(
        content=body,
        media_type="application/json",
        headers=_PRIVATE_READ,
    )
