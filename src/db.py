"""Database access for any SQL database SQLAlchemy can reach. SQLite, PostgreSQL, MySQL and MariaDB are built in.

This is the only module that opens database connections. Every session is read-only, and every query
runs with a time limit and a row cap, each set the way that database supports:

  sqlite      opened with mode=ro; a progress handler stops long queries
  postgresql  SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY; SET LOCAL statement_timeout
  mysql       SET SESSION TRANSACTION READ ONLY; SET SESSION max_execution_time (milliseconds)
  mariadb     SET SESSION TRANSACTION READ ONLY; SET SESSION max_statement_time (seconds)

MySQL and MariaDB sessions also turn on ANSI_QUOTES, so "double-quoted" identifiers work the same in all four.

Any other database with an installed SQLAlchemy dialect (Oracle, SQL Server, Snowflake, ...) also connects, but
gets neither a read-only session nor a time limit: is_guarded() is False, and the CLI says so. Connect to those
as a user with only SELECT rights. For production, do that everywhere.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, make_url

from src.utils import DB_PATH

DEFAULT_URL = f"sqlite:///{DB_PATH}"
ANSI_QUOTES = "SET SESSION sql_mode = CONCAT(@@SESSION.sql_mode, ',ANSI_QUOTES')"
SESSION_SETUP = {  # run once on every new connection
    "postgresql": ("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY",),
    "mysql": ("SET SESSION TRANSACTION READ ONLY", ANSI_QUOTES),
    "mariadb": ("SET SESSION TRANSACTION READ ONLY", ANSI_QUOTES),
}
TIMEOUT = {  # {ms} or {s}: the per-query limit
    "postgresql": "SET LOCAL statement_timeout = {ms}",
    "mysql": "SET SESSION max_execution_time = {ms}",
    "mariadb": "SET SESSION max_statement_time = {s}",
}
GUARDED = ("sqlite", "postgresql", "mysql", "mariadb")  # read-only session and time limit set here
ROW_ESTIMATE = {  # the planner's row count: instant, where COUNT(*) scans the table
    "postgresql": "SELECT c.reltuples::bigint FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relname = {table} AND n.nspname = COALESCE({schema}, current_schema())",
    "mysql": "SELECT TABLE_ROWS FROM information_schema.TABLES WHERE TABLE_NAME = {table} AND TABLE_SCHEMA = COALESCE({schema}, DATABASE())",
    "mariadb": "SELECT TABLE_ROWS FROM information_schema.TABLES WHERE TABLE_NAME = {table} AND TABLE_SCHEMA = COALESCE({schema}, DATABASE())",
}


class QueryError(RuntimeError):
    """A query failed, was rejected by the database, or ran past its time limit."""


def connect(url: str = DEFAULT_URL) -> Engine:
    """A read-only engine for the database at `url` (see is_guarded). Fails loudly if the database or its driver is missing."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        path = Path(parsed.database or "").expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"No SQLite database at {path}. Run ./setup.sh, or pass --db-url.")
        uri = f"{path.as_uri()}?mode=ro"  # a new connection per use (cheap for a local file, and safe across threads)
        return sa.create_engine(parsed.set(database=str(path)), poolclass=sa.pool.NullPool,
                                creator=lambda: sqlite3.connect(uri, uri=True, check_same_thread=False))
    try:
        engine = sa.create_engine(parsed, pool_pre_ping=True)
    except (ModuleNotFoundError, sa.exc.NoSuchModuleError) as exc:
        raise SystemExit(f"No SQLAlchemy driver for {parsed.drivername}: install its dialect package "
                         "(built in: sqlite, postgresql+psycopg, mysql+pymysql, mariadb+pymysql).") from exc
    sa.event.listen(engine, "connect", lambda dbapi_conn, _: _setup_session(dbapi_conn, dialect(engine)))
    return engine


def ping(engine: Engine) -> None:
    """Connect once, so a wrong URL or an unreachable server fails at startup with a clear message."""
    try:
        with engine.connect():
            pass
    except sa.exc.DBAPIError as exc:
        raise SystemExit(f"Could not connect to {describe(engine)}: {_message(exc.orig, 0)}") from exc


def is_guarded(engine: Engine) -> bool:
    """True when this module sets a read-only session and a query time limit for the database."""
    return dialect(engine) in GUARDED


def row_estimate(engine: Engine, table: str, schema: str | None) -> int | None:
    """The database's own row estimate, or None where there is none (SQLite and others count instead)."""
    sql = ROW_ESTIMATE.get(dialect(engine))
    if sql is None:
        return None
    literal = lambda v: "NULL" if v is None else "'" + v.replace("'", "''") + "'"
    value = scalar(engine, sql.format(table=literal(table), schema=literal(schema)), timeout_s=10)
    return None if value is None else int(value)


def dialect(engine: Engine) -> str:
    """'sqlite', 'postgresql', 'mysql' or 'mariadb'."""
    return engine.dialect.name


def describe(engine: Engine) -> str:
    """The URL without its password, for display and cache keys."""
    return engine.url.render_as_string(hide_password=True)


def quote(engine: Engine, name: str) -> str:
    """An identifier quoted for this database: "order" in SQLite and PostgreSQL, `order` in MySQL."""
    return engine.dialect.identifier_preparer.quote(name)


def query(engine: Engine, sql: str | sa.Executable, *, timeout_s: float, max_rows: int) -> tuple[tuple[str, ...], tuple[tuple, ...], bool]:
    """Run one read-only query. Returns (columns, rows, truncated). Raises QueryError on any database error.

    `sql` is the model's SQL text, or a SQLAlchemy statement (compiled here for this database, so internal
    queries such as schema inspection work everywhere). Text goes to the driver's own cursor with no
    parameters, so '%' in it (e.g. strftime('%Y', ...)) is never treated as a placeholder.
    """
    name = dialect(engine)
    if not isinstance(sql, str):
        sql = str(sql.compile(engine, compile_kwargs={"literal_binds": True}))
    with engine.connect() as conn:
        raw = conn.connection.driver_connection
        cursor = raw.cursor()
        try:
            if name == "sqlite":
                deadline = time.monotonic() + timeout_s
                raw.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
            elif name in TIMEOUT:
                cursor.execute(TIMEOUT[name].format(ms=int(timeout_s * 1000), s=timeout_s))
            cursor.execute(sql)
            columns = tuple(d[0] for d in cursor.description or ())
            rows = cursor.fetchmany(max_rows + 1)
        except engine.dialect.loaded_dbapi.Error as exc:
            raise QueryError(_message(exc, timeout_s)) from exc
        finally:
            if name == "sqlite":
                raw.set_progress_handler(None, 0)
            cursor.close()
    return columns, tuple(tuple(r) for r in rows[:max_rows]), len(rows) > max_rows


def scalar(engine: Engine, sql: str, timeout_s: float = 120.0) -> Any:
    _, rows, _ = query(engine, sql, timeout_s=timeout_s, max_rows=1)
    return rows[0][0] if rows else None


def _setup_session(dbapi_conn: Any, name: str) -> None:
    cursor = dbapi_conn.cursor()
    for statement in SESSION_SETUP.get(name, ()):
        cursor.execute(statement)
    cursor.close()
    dbapi_conn.commit()  # in PostgreSQL a SET inside a rolled-back transaction would be undone


def _message(exc: Exception, timeout_s: float) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    stopped = ("interrupted", "statement timeout", "canceling statement", "max_execution_time", "max_statement_time")
    if any(s in text.lower() for s in stopped):
        return f"query took longer than {timeout_s:.0f}s and was stopped"
    return text
