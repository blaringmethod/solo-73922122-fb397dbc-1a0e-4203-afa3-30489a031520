"""Database engine, session factory and schema creation.

Two dialects are supported:

* PostgreSQL (production): normal MVCC with row locks (``SELECT ... FOR UPDATE``).
* SQLite (single-file, possibly shared by several API instances): the file is
  the serialization point. To make SQLite behave transactionally the driver
  must run in *autocommit* (``isolation_level=None``) with explicit ``BEGIN`` /
  ``BEGIN IMMEDIATE`` statements. Under pysqlite's legacy implicit mode
  (``isolation_level=""``) a DDL/DML statement starts an implicit transaction
  but ``SAVEPOINT`` does not, so releasing the outermost savepoint silently
  commits work that a later ``ROLLBACK`` was supposed to discard. That defect
  is what let rejected bundles leak rows, and deferred ``BEGIN`` allowed two
  instances to take conflicting read snapshots with no write serialization.

``sqlite_write`` is a per-connection execution option: the write engine opens
transactions with ``BEGIN IMMEDIATE`` so the writer lock is acquired up front,
serializing writers and preventing snapshot-upgrade write skew across
processes.
"""
from __future__ import annotations

import time
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        return {
            "connect_args": {
                "timeout": settings.SQLITE_BUSY_TIMEOUT_SECONDS,
                "check_same_thread": False,
            }
        }
    return {"pool_pre_ping": True}


def _install_sqlite_hooks(target_engine) -> None:
    """Attach explicit-transaction + WAL handlers to a SQLite engine."""

    @event.listens_for(target_engine, "connect")
    def _sqlite_on_connect(dbapi_connection, _record):  # pragma: no cover - driver hook
        # Take control away from pysqlite's implicit transaction handling.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={settings.SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    @event.listens_for(target_engine, "begin")
    def _sqlite_on_begin(conn):  # pragma: no cover - driver hook
        if conn.get_execution_options().get("sqlite_write"):
            # Acquire the write lock now: concurrent writers queue here instead
            # of upgrading a stale read snapshot later (avoids write skew).
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            conn.exec_driver_sql("BEGIN")


def build_engine(url: str):
    """Create an engine with the correct transaction semantics for ``url``."""
    created = create_engine(url, future=True, **_engine_kwargs(url))
    if url.startswith("sqlite"):
        _install_sqlite_hooks(created)
    return created


engine = build_engine(settings.DATABASE_URL)

# SQLite: every write goes through an OptionEngine whose transactions start
# with BEGIN IMMEDIATE. PostgreSQL: the same engine (row locks do the work).
write_engine = (
    engine.execution_options(sqlite_write=True)
    if settings.DATABASE_URL.startswith("sqlite")
    else engine
)

SessionLocal = sessionmaker(
    bind=write_engine, autoflush=False, expire_on_commit=False, future=True
)
ReadSessionLocal = sessionmaker(
    bind=engine, autoflush=False, expire_on_commit=False, future=True
)


def is_sqlite(session_or_engine: object) -> bool:
    bind = getattr(session_or_engine, "bind", None) or session_or_engine
    dialect = getattr(bind, "dialect", None)
    return dialect is not None and dialect.name == "sqlite"


def is_busy_error(exc: BaseException) -> bool:
    """True for transient SQLite lock/snapshot errors that are safe to retry."""
    if not isinstance(exc, OperationalError):
        return False
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "database is locked",
            "database is busy",
            "busy_snapshot",
            "database table is locked",
            "cannot start a transaction within a transaction",
            "sqlite_busy",
            "sqlite_locked",
        )
    )


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one *write* session per request, rollback on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_read_session() -> Iterator[Session]:
    """FastAPI dependency for GET endpoints: read-only, no writer lock."""
    session = ReadSessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db(bind=None) -> None:
    # Import models so they register on the metadata before create_all.
    from app import models

    assert models.Base is Base
    target = bind if bind is not None else write_engine
    # Several API containers may run this concurrently against one SQLite file.
    # BEGIN IMMEDIATE + busy_timeout serializes them; if a table-exists race
    # still surfaces, re-running create_all (checkfirst) succeeds.
    for attempt in range(1, settings.DB_BUSY_RETRIES + 1):
        try:
            Base.metadata.create_all(bind=target)
            return
        except OperationalError as exc:
            if not is_busy_error(exc) or attempt == settings.DB_BUSY_RETRIES:
                raise
            time.sleep(settings.DB_BUSY_BACKOFF_SECONDS * attempt)
