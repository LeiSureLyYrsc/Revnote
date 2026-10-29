"""Declarative base and constraint naming convention for Revnote models.

Schema truth lives in Alembic revisions: **``create_all()`` is never called**
anywhere in this project. The metadata declared here exists so that

* revisions build tables through ``op.create_table(...)*`` with the *same*
  constraint names the ORM metadata implies (so ``alembic check`` stays
  diff-clean), and
* every constraint gets a stable, reviewable, deterministic name.

Naming convention notes (Phase 2 fix):

* ``column_0_list_name`` **is not a token SQLAlchemy understands** — a
  convention containing it raises ``KeyError`` as soon as an index, unique or
  check constraint is named. The multi-column tokens are
  ``column_0_N_name`` (``_``-joined list of names, e.g. ``workspace_id_seq``)
   and ``column_0_name`` (the first column only, e.g. ``workspace_id``).
* ``ck`` keeps ``%(constraint_name)s``: **every** ``CheckConstraint`` must be
  explicitly named, or SQLAlchemy raises ``InvalidRequestError``. Naming CHECKs
  explicitly is required here anyway — PostgreSQL reflects check expressions
  re-formatted (extra parentheses), so the name is the stable identity.
* Composite foreign keys deliberately use the short
  ``fk_<table>_<first column>_<referred table>`` form. Two composite FKs on the
  same table can share that prefix (e.g. ``folders`` references ``workspaces``
  and ``folders``), so every ``ForeignKeyConstraint`` in the models **and** the
  revisions is named explicitly; the convention is only a safety net.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

#: Constraint naming convention shared by ORM metadata and Alembic revisions
#: (Alembic's ``op.*`` helpers inherit ``naming_convention`` from
#: ``target_metadata``, so both sides derive identical names).
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for the Revnote domain models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


__all__ = ["NAMING_CONVENTION", "Base"]
