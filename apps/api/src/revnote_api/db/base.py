"""Declarative base for future Revnote models.

Scaffold scope: **no business tables are declared here** and no ``create_all()``
is ever called — schema comes exclusively from Alembic revisions.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

#: Constraint naming convention so future migrations get stable, reviewable
#: names (and Alembic autogenerate stays diff-clean).
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_list_name)s",
    "uq": "uq_%(table_name)s_%(column_0_list_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for the (future) Revnote domain models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
