"""``services`` — the only writers of the six kernel tables.

Public surface: :class:`revnote_api.services.sync_write.SyncWriteService`, one
method (``execute``) taking one :class:`~revnote_api.kernel.commands.Command` and
returning one :class:`~revnote_api.kernel.results.WriteResult`.

Nothing here imports FastAPI, HTTP or any adapter, and nothing here retries a
rejected write. :class:`~revnote_api.services.sync_repository.SyncRepository` is
an internal detail; it is importable for tests that need to inject a fault *after*
a real statement ran, but it is not part of the public API.
"""

from __future__ import annotations

from revnote_api.services.sync_repository import SyncRepository
from revnote_api.services.sync_write import LOCK_TIMEOUT_MS, SyncWriteService

__all__ = ["LOCK_TIMEOUT_MS", "SyncRepository", "SyncWriteService"]
