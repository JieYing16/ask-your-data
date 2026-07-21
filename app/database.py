import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

import duckdb

logger = logging.getLogger("ask_your_data.database")

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "chinook.db"
MAX_ROWS = 200

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|import|pragma|call|vacuum|checkpoint)\b",
    re.IGNORECASE,
)

# The schema is static per process, and a single DuckDB connection can be reused
# across all requests, so we compute each once and guard the shared connection
# with a lock (DuckDB connections are not safe for concurrent use).
_schema_ddl_cache: str | None = None
_duckdb_con: duckdb.DuckDBPyConnection | None = None
_duckdb_lock = threading.Lock()


class QueryError(Exception):
    pass


def get_schema_ddl() -> str:
    """Return the CREATE TABLE statements for every table, including foreign keys.

    The result is cached after the first call — the schema does not change while
    the process is running, so we avoid re-opening SQLite on every LLM iteration.
    """
    global _schema_ddl_cache
    if _schema_ddl_cache is None:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.cursor()
            cur.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name")
            _schema_ddl_cache = "\n\n".join(row[0] for row in cur.fetchall() if row[0])
        finally:
            conn.close()
    return _schema_ddl_cache


def list_tables() -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _get_connection() -> duckdb.DuckDBPyConnection:
    """Return the shared DuckDB connection, creating it on first use.

    Callers must hold ``_duckdb_lock`` while using the returned connection. The
    connection is created once — installing/loading the sqlite extension and
    attaching the database is expensive, so we do not want to repeat it per query.
    """
    global _duckdb_con
    if _duckdb_con is None:
        con = duckdb.connect()
        con.execute("INSTALL sqlite; LOAD sqlite;")
        con.execute(f"ATTACH '{DB_PATH.as_posix()}' AS chinook (TYPE sqlite, READ_ONLY)")
        con.execute("USE chinook")
        _duckdb_con = con
    return _duckdb_con


def validate_select(sql: str) -> str:
    """Reject anything that isn't a single read-only SELECT/WITH statement."""
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise QueryError("Empty query.")
    if ";" in cleaned:
        raise QueryError("Only a single statement is allowed.")
    if not re.match(r"^(select|with)\b", cleaned, re.IGNORECASE):
        raise QueryError("Only SELECT statements are allowed.")
    if _FORBIDDEN.search(cleaned):
        raise QueryError("Query contains a disallowed keyword. Only read-only SELECT queries are permitted.")
    return cleaned


def execute_query(sql: str) -> dict:
    """Validate and run a SELECT query, returning columns, rows, and row count."""
    cleaned = validate_select(sql)
    started = time.perf_counter()
    with _duckdb_lock:
        con = _get_connection()
        try:
            result = con.execute(cleaned)
            columns = [d[0] for d in result.description]
            rows = result.fetchall()
        except duckdb.Error as exc:
            raise QueryError(str(exc)) from exc
    logger.info("execute_sql took %.1f ms (%d rows)", (time.perf_counter() - started) * 1000, len(rows))

    truncated = len(rows) > MAX_ROWS
    limited_rows = rows[:MAX_ROWS]
    return {
        "columns": columns,
        "rows": [list(r) for r in limited_rows],
        "row_count": len(rows),
        "truncated": truncated,
    }
