"""Engine creation, Alembic migration runner, and session helper.

sqlite-vec is loaded opportunistically on every connection; semantic search
degrades gracefully when the extension is unavailable.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def _try_load_vec(conn: sqlite3.Connection) -> None:
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception:
        pass  # semantic search is optional; fuzzy search still works


def make_engine(db_path: Path | str, *, echo: bool = False) -> Engine:
    engine = create_engine(f"sqlite:///{db_path}", echo=echo)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
        _try_load_vec(dbapi_conn)

    return engine


def migrate(db_path: Path | str) -> None:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")


def init_db(db_path: Path | str, *, echo: bool = False) -> Engine:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    migrate(db_path)
    return make_engine(db_path, echo=echo)


def vec_available(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT vec_version()"))
        return True
    except Exception:
        return False


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
