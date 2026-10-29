"""The one place these tests reach production's write service.

``SyncWriteService(session_factory).execute(command) -> WriteResult`` is the documented
interface (plan section 6), so it is imported and called directly: no attribute lookup, no
fallback spelling, no adapter.
"""

from __future__ import annotations

from typing import Any

from revnote_api.services.sync_write import SyncWriteService

__all__ = ["SyncWriteService", "call"]


async def call(service: SyncWriteService, command: Any) -> Any:
    """Apply *command* and return its ``WriteResult`` (or raise a contract error)."""
    return await service.execute(command)
