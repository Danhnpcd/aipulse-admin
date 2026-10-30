from collections.abc import Generator
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


def build_engine(database_url: str | None = None):
    url = (database_url or get_settings().DATABASE_URL).strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    is_sqlite = url.startswith("sqlite")
    if is_sqlite and ":///" in url and ":memory:" not in url:
        sqlite_path_str = url.split(":///", 1)[1].split("?", 1)[0]
        if sqlite_path_str:
            try:
                Path(sqlite_path_str).parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

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
