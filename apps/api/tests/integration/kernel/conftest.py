"""Fixtures for the Phase 2 Sync Kernel integration tests.

The PostgreSQL harness lives in ``apps/api/tests/integration/conftest.py`` and is reused
unchanged; this module adds only the kernel wiring on top of it.

Fixtures (all function scoped, so no case can leak into another):

* ``kernel_db`` — :class:`kernel_support.KernelDB` over the business database;
* ``seed`` — async callable returning one **new** ``(user, workspace)`` per call;
* ``write`` — async callable ``(command) -> WriteResult`` over one service instance.

Isolation model: every ``seed()`` call inserts a brand-new user and workspace, so cases never
share rows and never truncate anything (plan section 8). Every service call gets its own
``AsyncSession`` from the harness' ``NullPool`` engine, which is what makes the
multi-connection cases work without hitting a pool ceiling.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from kernel_service import SyncWriteService
from kernel_service import call as kernel_call
from kernel_support import KernelDB

#: ``await seed() -> (user, workspace)``
Seed = Callable[..., Awaitable[tuple[Any, Any]]]
WriteCall = Callable[[Any], Awaitable[Any]]


@pytest.fixture
def kernel_db(session_factory: Any) -> KernelDB:
    """Direct database access on the business database (a fresh session per query)."""
    return KernelDB(session_factory)


@pytest.fixture
def seed(seed_workspace: Seed) -> Seed:
    """One brand-new ``(user, workspace)`` per call (no truncation, no shared rows)."""
    return seed_workspace


@pytest.fixture
def write(session_factory: Any) -> WriteCall:
    """``SyncWriteService.execute`` over the independent-session factory."""
    service = SyncWriteService(session_factory)

    async def perform(command: Any) -> Any:
        return await kernel_call(service, command)

    return perform


@pytest.fixture
def new_write(session_factory: Any) -> Callable[[], WriteCall]:
    """A callable backed by a *fresh* service instance.

    Races and retries must not share a service object: plan section 6 gives every call an
    independent ``AsyncSession``, and a shared instance would make a green test say nothing
    about that promise.
    """

    def build() -> WriteCall:
        service = SyncWriteService(session_factory)

        async def perform(command: Any) -> Any:
            return await kernel_call(service, command)

        return perform

    return build
