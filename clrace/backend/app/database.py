"""Database engine, session, init."""
from pathlib import Path

from sqlalchemy import event
from sqlmodel import SQLModel, Session, create_engine

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "clrace2026.db"

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    connect_args={"check_same_thread": False, "timeout": 15},
    pool_size=20,
    max_overflow=30,
    pool_timeout=30,
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _rec):
    """WAL + a real busy timeout.

    Defaults (journal_mode=delete, busy_timeout=0) make concurrent writers fail
    with 'database is locked' instantly; the pool then fills with stuck
    connections and every request dies on 'QueuePool limit of size 5 overflow 10'.
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=15000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as s:
        yield s
