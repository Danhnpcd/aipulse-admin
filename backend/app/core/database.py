from collections.abc import Generator
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


def build_engine(database_url: str | None = None):
    url = database_url or get_settings().DATABASE_URL
    is_sqlite = url.startswith("sqlite")
    connect_args = {"check_same_thread": False, "timeout": 30.0} if is_sqlite else {}
    pool_kwargs = {"poolclass": NullPool} if is_sqlite else {"pool_pre_ping": True}
    eng = create_engine(url, connect_args=connect_args, future=True, **pool_kwargs)

    if is_sqlite:
        @event.listens_for(eng, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.close()

    return eng


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def configure_database(database_url: str) -> None:
    """Rebinds engine and SessionLocal (used for isolated automated test suites)."""
    global engine, SessionLocal
    engine.dispose()
    engine = build_engine(database_url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
