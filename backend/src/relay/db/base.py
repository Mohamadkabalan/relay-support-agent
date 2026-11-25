"""Declarative base and column conventions shared by every table."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Dialect, MetaData, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

# Deterministic constraint names, so Alembic migrations can refer to them.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 (SQLAlchemy reads this class attribute)
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
    }


def uuid_pk() -> Mapped[uuid.UUID]:
    """A UUIDv7 primary key.

    SQLAlchemy fills it with `uuid.uuid7` (new in Python 3.14) when the row is flushed; raw SQL
    inserts get Postgres 18's `uuidv7()`. Both are time-ordered, so new rows sort last.
    """
    return mapped_column(primary_key=True, default=uuid.uuid7, server_default=text("uuidv7()"))


class TextEnum[E: StrEnum](TypeDecorator[E]):
    """A StrEnum stored as text (with a CHECK constraint) and loaded back as the enum member.

    Native Postgres ENUM types are awkward to migrate; text plus a CHECK constraint is not.
    """

    impl = Text
    cache_ok = True

    def __init__(self, enum: type[E]) -> None:
        super().__init__()
        self.enum = enum

    def process_bind_param(self, value: E | str | None, dialect: Dialect) -> str | None:
        return None if value is None else self.enum(value).value

    def process_result_value(self, value: str | None, dialect: Dialect) -> E | None:
        return None if value is None else self.enum(value)
