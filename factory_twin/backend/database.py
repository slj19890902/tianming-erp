from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def create_database_engine(database_url: str) -> Engine:
    connect_args: dict[str, object] = {}
    engine_options: dict[str, object] = {"future": True}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        if database_url == "sqlite://" or ":memory:" in database_url:
            engine_options["poolclass"] = StaticPool
        elif "///" in database_url:
            database_path = Path(database_url.split("///", 1)[1])
            database_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(database_url, connect_args=connect_args, **engine_options)


def create_session_factory(engine: Engine):
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
