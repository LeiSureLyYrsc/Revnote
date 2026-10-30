"""Joplin's serialized item format: ``title``, blank line, ``body``, blank line, props.

The format is defined by the client's ``BaseItem.serialize``/``unserialize`` and
is what a real client PUTs and expects to GET:

* a note item is ``title + "\\n\\n" + body + "\\n\\n" + props`` (either part may be
  empty, and ``serialize`` omits an empty body entirely);
* a folder or resource item has no body, so it is ``title + "\\n\\n" + props``;
* the parser scans **from the bottom** for the first blank line; everything
  above it is the header, whose first line is the title and — for a note — whose
  remaining lines (after the separating blank) are the body;
* every property is ``key: value``, one per line, and ``type_`` is required.

Only the properties the bridge actually maps are kept: ``id``, ``parent_id``,
``type_``, ``created_time``, ``updated_time`` and the resource metadata
(``mime``, ``filename``, ``size``, ``file_extension``). Unknown properties are
accepted and ignored — the client sends many, and refusing them would make the
bridge brittle against a client that adds a field.

Timestamps travel as ``YYYY-MM-DDTHH:mm:ss.SSSZ`` (UTC). A value that cannot be
parsed is dropped rather than guessed: the server is the authority on time.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "TYPE_FOLDER",
    "TYPE_NOTE",
    "TYPE_RESOURCE",
    "JoplinItem",
    "JoplinItemError",
    "format_millis",
    "parse_item",
    "serialize_folder",
    "serialize_note",
    "serialize_resource",
]

TYPE_NOTE: Final[int] = 1
TYPE_FOLDER: Final[int] = 2
TYPE_RESOURCE: Final[int] = 3

#: The item kinds this bridge maps onto a native entity.
SUPPORTED_TYPES: Final[frozenset[int]] = frozenset({TYPE_NOTE, TYPE_FOLDER, TYPE_RESOURCE})

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?Z$")


class JoplinItemError(ValueError):
    """A body that cannot be a Joplin item. Rendered as ``400 bad_request``."""


def _parse_time(value: str) -> int | None:
    """``2026-10-30T12:00:00.000Z`` -> epoch milliseconds, or ``None``."""
    match = _ISO_RE.fullmatch(value.strip())
    if match is None:
        return None
    year, month, day, hour, minute, second, fraction = match.groups()
    try:
        moment = datetime.datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second),
            tzinfo=datetime.UTC,
        )
    except ValueError:
        return None
    millis = int((fraction or "0").ljust(3, "0")[:3])
    return int(moment.timestamp()) * 1000 + millis


def format_millis(value: datetime.datetime | None) -> str:
    """Render one timestamp the way the client does (UTC, milliseconds)."""
    if value is None:
        return ""
    if value.tzinfo is None:  # pragma: no cover - the schema always stores aware
        value = value.replace(tzinfo=datetime.UTC)
    utc = value.astimezone(datetime.UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


@dataclass(frozen=True, slots=True)
class JoplinItem:
    """The parts of a Joplin item the bridge maps."""

    id: str
    type_: int
    title: str
    body: str = ""
    parent_id: str = ""
    created_time: int | None = None
    updated_time: int | None = None
    props: dict[str, str] = field(default_factory=dict)

    @property
    def resource_mime(self) -> str:
        return (self.props.get("mime") or "").strip()

    @property
    def resource_name(self) -> str | None:
        value = (self.props.get("filename") or "").strip()
        return value or None

    @property
    def resource_size(self) -> int | None:
        raw = (self.props.get("size") or "").strip()
        if not raw.isdigit():
            return None
        size = int(raw)
        return size if size > 0 else None


def parse_item(content: str) -> JoplinItem:
    """Parse one serialized item; raise :class:`JoplinItemError` on nonsense."""
    if not isinstance(content, str):
        raise JoplinItemError("item content must be text")
    lines = content.split("\n")
    props: dict[str, str] = {}
    header_end = 0

    for index in range(len(lines) - 1, -1, -1):
        line = lines[index].strip()
        if line == "":
            header_end = index
            break
        separator = line.find(":")
        if separator < 0:
            raise JoplinItemError("invalid property line")
        props[line[:separator].strip()] = line[separator + 1 :].strip()

    raw_type = props.get("type_")
    if raw_type is None or not raw_type.strip().isdigit():
        raise JoplinItemError("missing type_")
    type_ = int(raw_type.strip())
    if type_ not in SUPPORTED_TYPES:
        raise JoplinItemError("unsupported item type")

    identifier = props.get("id", "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise JoplinItemError("invalid item id")

    header = lines[:header_end]
    title = header[0] if header else ""
    body = ""
    if type_ == TYPE_NOTE and len(header) > 2:
        # header = [title, "", body...] — a note without a body has no third line.
        body = "\n".join(header[2:])

    return JoplinItem(
        id=identifier,
        type_=type_,
        title=title,
        body=body,
        parent_id=props.get("parent_id", "").strip().lower(),
        created_time=_parse_time(props.get("created_time", "")),
        updated_time=_parse_time(props.get("updated_time", "")),
        props=props,
    )


def _render(title: str, body: str | None, props: list[tuple[str, str]]) -> str:
    parts: list[str] = [title]
    if body:
        parts.append(body)
    parts.append("\n".join(f"{key}: {value}" for key, value in props))
    return "\n\n".join(parts)


def serialize_note(
    *,
    jop_key: str,
    title: str,
    body: str,
    parent_key: str,
    created_time: datetime.datetime | None,
    updated_time: datetime.datetime | None,
) -> str:
    """Render a note item the client can ``unserialize``."""
    return _render(
        title,
        body,
        [
            ("id", jop_key),
            ("parent_id", parent_key),
            ("created_time", format_millis(created_time)),
            ("updated_time", format_millis(updated_time)),
            ("user_created_time", format_millis(created_time)),
            ("user_updated_time", format_millis(updated_time)),
            ("markup_language", "1"),
            ("type_", str(TYPE_NOTE)),
        ],
    )


def serialize_folder(
    *,
    jop_key: str,
    title: str,
    parent_key: str,
    created_time: datetime.datetime | None,
    updated_time: datetime.datetime | None,
) -> str:
    """Render a folder item (no body; the client parses ``title`` only)."""
    return _render(
        title,
        None,
        [
            ("id", jop_key),
            ("parent_id", parent_key),
            ("created_time", format_millis(created_time)),
            ("updated_time", format_millis(updated_time)),
            ("user_created_time", format_millis(created_time)),
            ("user_updated_time", format_millis(updated_time)),
            ("type_", str(TYPE_FOLDER)),
        ],
    )


def serialize_resource(
    *,
    jop_key: str,
    title: str,
    mime: str,
    filename: str,
    size: int,
    created_time: datetime.datetime | None,
    updated_time: datetime.datetime | None,
) -> str:
    """Render a resource metadata item (the blob lives at ``.resource/<key>``)."""
    extension = filename.rsplit(".", 1)[-1] if "." in filename else ""
    return _render(
        title,
        None,
        [
            ("id", jop_key),
            ("mime", mime),
            ("filename", filename),
            ("size", str(size)),
            ("file_extension", extension),
            ("created_time", format_millis(created_time)),
            ("updated_time", format_millis(updated_time)),
            ("type_", str(TYPE_RESOURCE)),
        ],
    )
