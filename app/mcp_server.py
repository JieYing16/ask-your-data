"""MCP server wrapper (SKETCH) — exposes the query engine over the Model Context Protocol.

This reuses the exact same validated, read-only query engine as the web app
(`app/database.py`), so any MCP client — Claude Desktop, Claude Code, or another
agent — can explore the database with the same safety guarantees the HTTP API has.

It is intentionally standalone and optional: it adds one dependency, `mcp`, that
the web app itself does not need.

    pip install "mcp[cli]"

Run it directly over stdio (how MCP clients launch it):

    python -m app.mcp_server

Register it with an MCP client (e.g. Claude Desktop's config) roughly like:

    {
      "mcpServers": {
        "ask-your-data": {
          "command": "python",
          "args": ["-m", "app.mcp_server"],
          "cwd": "/path/to/ask-your-data",
          "env": { "DB_PATH": "data/chinook.db" }
        }
      }
    }

Because DB_PATH is honored by `app.database`, the same server points at any
SQLite database without code changes.
"""

from mcp.server.fastmcp import FastMCP

from app.database import DB_PATH, MAX_ROWS, QueryError, execute_query, get_schema_ddl, list_tables

mcp = FastMCP("ask-your-data")


@mcp.tool()
def execute_sql(query: str) -> dict:
    """Run a single read-only SQL SELECT/WITH statement and return the rows.

    Only SELECT/WITH is permitted; anything else is rejected by the same
    validation the web app uses. Results are capped at MAX_ROWS rows.

    Returns a dict with `columns`, `rows`, `row_count`, and `truncated`, or an
    `error` string if the query was invalid or failed.
    """
    try:
        return execute_query(query)
    except QueryError as exc:
        return {"error": str(exc)}


@mcp.tool()
def list_database_tables() -> list[str]:
    """List the names of every table in the database."""
    return list_tables()


@mcp.resource("schema://ddl")
def schema_ddl() -> str:
    """The full CREATE TABLE DDL (including foreign keys) for the database.

    Exposed as an MCP *resource* so a client can load it as context up front,
    mirroring how the web app injects the schema into its system prompt.
    """
    return get_schema_ddl()


@mcp.prompt()
def ask(question: str) -> str:
    """A ready-made prompt template for answering a question against this database."""
    return (
        f"Using the `execute_sql` tool against {DB_PATH.name} (max {MAX_ROWS} rows "
        f"per query), answer this question and show the SQL you ran:\n\n{question}"
    )


if __name__ == "__main__":
    mcp.run()
