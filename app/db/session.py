from contextlib import contextmanager
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(settings().database_url, pool_pre_ping=True, future=True)
    return _engine


def session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=engine(), expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    s = session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
