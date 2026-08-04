from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

DATABASE_URL_ENV = "GOLIATH_DATABASE_URL"


def create_production_engine(database_url: str | None = None) -> Engine:
    """Create a production engine, refusing anything other than PostgreSQL."""
    url = database_url or os.getenv(DATABASE_URL_ENV)
    if not url:
        raise RuntimeError(f"{DATABASE_URL_ENV} must be set for production")
    if make_url(url).get_backend_name() != "postgresql":
        raise ValueError("production databases must use PostgreSQL")
    return create_engine(url, pool_pre_ping=True)


def create_test_engine(database_url: str = "sqlite+pysqlite:///:memory:") -> Engine:
    """Create an isolated SQLite engine intended only for tests."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        raise ValueError("test databases must use SQLite")

    kwargs: dict[str, object] = {}
    if url.database in (None, "", ":memory:"):
        kwargs.update(connect_args={"check_same_thread": False}, poolclass=StaticPool)
    engine = create_engine(url, **kwargs)

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Provide transaction ownership outside repositories."""
    session = factory()
    try:
        with session.begin():
            yield session
    finally:
        session.close()
