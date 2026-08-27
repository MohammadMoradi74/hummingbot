import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_PAIR_TRADE_SCHEMA_PATH = Path(__file__).with_name("pair_trade_schema.sql")


class MetricsDatabase:
    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError(
                "MM_METRICS_DATABASE_URL is not set. "
                "Example: postgresql+psycopg://user:pass@localhost:5432/hummingbot"
            )
        self._engine: Engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=4,
        )
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        db_session = self._session_factory()
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise
        finally:
            db_session.close()

    def execute(self, sql: str, params: Optional[dict] = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(sql), params or {})

    def ping(self) -> bool:
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            logger.warning("Metrics database ping failed: %s", exc)
            return False


def apply_schema(database_url: str) -> None:
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    db = MetricsDatabase(database_url)
    with db._engine.begin() as conn:
        for statement in _split_sql_statements(schema_sql):
            conn.execute(text(statement))
    logger.info("Applied metrics schema from %s", _SCHEMA_PATH)


def apply_pair_trade_schema(database_url: str) -> None:
    schema_sql = _PAIR_TRADE_SCHEMA_PATH.read_text(encoding="utf-8")
    db = MetricsDatabase(database_url)
    with db._engine.begin() as conn:
        for statement in _split_sql_statements(schema_sql):
            conn.execute(text(statement))
    logger.info("Applied pair-trade metrics schema from %s", _PAIR_TRADE_SCHEMA_PATH)


def apply_all_schemas(database_url: str) -> None:
    apply_schema(database_url)
    apply_pair_trade_schema(database_url)


def _split_sql_statements(sql: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        current.append(line)
        if stripped.endswith(";"):
            statements.append("\n".join(current))
            current = []
    if current:
        statements.append("\n".join(current))
    return statements
