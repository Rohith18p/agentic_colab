"""Engine and session setup.

Everything goes through SQLAlchemy so we can move to Postgres later. The
SQLite-specific bits (WAL, busy timeout, BEGIN IMMEDIATE) are confined to
`make_engine`.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        return create_engine(database_url, pool_pre_ping=True)

    if url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):  # pragma: no cover - trivial
        # Take over transaction control from pysqlite so we can issue
        # BEGIN IMMEDIATE ourselves (see _on_begin).
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn):  # pragma: no cover - trivial
        # Every transaction takes the write lock up front. Writers queue on the
        # busy timeout instead of failing with SQLITE_BUSY on a lock upgrade.
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine)
