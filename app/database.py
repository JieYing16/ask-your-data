import re
import sqlite3
from pathlib import Path

import duckdb

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "chinook.db"
MAX_ROWS = 200

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|import|pragma|call|vacuum|checkpoint)\b",
    re.IGNORECASE,
)


class QueryError(Exception):
    pass


def get_schema_ddl() -> str:
    """Return the CREATE TABLE statements for every table, including foreign keys."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name")
        return "\n\n".join(row[0] for row in cur.fetchall() if row[0])
    finally:
        conn.close()


def list_tables() -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL sqlite; LOAD sqlite;")
    con.execute(f"ATTACH '{DB_PATH.as_posix()}' AS chinook (TYPE sqlite, READ_ONLY)")
    con.execute("USE chinook")
    return con


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
    con = _connect()
    try:
        result = con.execute(cleaned)
        columns = [d[0] for d in result.description]
        rows = result.fetchall()
    except duckdb.Error as exc:
        raise QueryError(str(exc)) from exc
    finally:
        con.close()

    truncated = len(rows) > MAX_ROWS
    limited_rows = rows[:MAX_ROWS]
    return {
        "columns": columns,
        "rows": [list(r) for r in limited_rows],
        "row_count": len(rows),
        "truncated": truncated,
    }
