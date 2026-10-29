"""HTTP layer: one router entry, one error surface, bounded request intake.

This package deliberately does **not** re-export ``router``: a name in
``__init__`` would shadow the :mod:`revnote_api.api.router` *module*, and
``import revnote_api.api.router`` would then hand back an ``APIRouter`` instead.
Import the module path explicitly - ``from revnote_api.api.router import router``
- which is also how ``main.py`` mounts it.

Layout::

    api/
    ├── errors.py        error vocabulary, kernel mapping, handlers
    ├── router.py        the single registration entry (``/api/v1`` prefix)
    ├── middleware/      request id, body limits, push budget
    ├── schemas/         wire models (``common`` now, per-endpoint later)
    └── routes/          health now, auth/sync/read later
"""

from __future__ import annotations
