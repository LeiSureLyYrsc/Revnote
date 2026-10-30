"""Joplin compatibility adapter (Phase 6).

This package is the **only** place in the tree that knows Joplin's protocol. It
translates requests into calls on ``kernel`` / ``services`` / the attachment
port, and translates their answers back into the shapes a Joplin client
understands. Nothing below it — not the kernel, not ``services/sync_*`` — may
import this package; the boundary test in ``tests/unit/test_kernel_boundaries``
already enforces that direction.

Layout::

    ids.py        32-hex Joplin keys <-> native UUIDs
    serialize.py  Joplin's title/body/props item text format
    delta.py      change_events -> compressed Joplin delta (pure function)
    session.py    POST /api/sessions, opaque session rows, request auth
    locks.py      sync/exclusive locks
    repository.py SQL used by the item routes
    resources.py  .resource/<key> blobs -> the Phase 5 attachment service
    routes.py     the HTTP surface, mounted only when JOPLIN_BRIDGE_ENABLED
    errors.py     Joplin's ``{"error", "code"}`` envelope and its mapping table
"""
