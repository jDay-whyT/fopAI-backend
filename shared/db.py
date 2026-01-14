from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from google.cloud.sql.connector import Connector
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from shared.settings import settings

_connector: Connector | None = None
_engine: Engine | None = None


def _create_connector() -> Connector:
    global _connector
    if _connector is None:
        _connector = Connector()
    return _connector


def _create_engine() -> Engine:
    if settings.database_url:
        return create_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_recycle=300,
            pool_timeout=10,
            pool_size=1,
            max_overflow=0,
        )

    if not settings.db_instance_connection_name:
        raise RuntimeError("DB_INSTANCE_CONNECTION_NAME is required when DATABASE_URL is not set")

    connector = _create_connector()

    def get_conn():
        return connector.connect(
            settings.db_instance_connection_name,
            "pg8000",
            user=settings.db_user,
            password=settings.db_password,
            db=settings.db_name,
        )

    return create_engine(
        "postgresql+pg8000://",
        creator=get_conn,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_timeout=10,
        pool_size=1,
        max_overflow=0,
    )


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _create_engine()
    return _engine


@contextmanager
def db_session() -> Iterator:
    engine = get_engine()
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
        transaction.commit()
    except Exception:
        transaction.rollback()
        raise
    finally:
        connection.close()
