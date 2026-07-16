import json
import os

import anthropic

from app.database import QueryError, execute_query, get_schema_ddl

MODEL_NAME = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
MAX_TOOL_ITERATIONS = 4

EXECUTE_SQL_TOOL = {
    "name": "execute_sql",
    "description": (
        "Run a single read-only SQL SELECT statement against the Chinook digital "
        "music store database and return the resulting rows. Use this to explore "
        "the data and to compute the final answer. Only SELECT/WITH statements "
        "are permitted."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A single read-only SQL SELECT statement.",
            }
        },
        "required": ["query"],
    },
}


def _system_prompt() -> str:
    schema = get_schema_ddl()
    return f"""You are a data analyst assistant for the "Chinook" digital music store database.

You answer natural-language questions by writing and running SQL SELECT queries \
against the database via the execute_sql tool, then explaining the result in plain \
language.

Database schema (SQLite DDL):

{schema}

Rules:
- Always call execute_sql at least once before answering — never guess at numbers.
- Only SELECT/WITH statements are allowed; the tool rejects anything else.
- Table and column names are case-sensitive and use the exact names in the schema \
above (e.g. "Customer", "InvoiceLine").
- Prefer aggregate queries (COUNT, SUM, AVG, GROUP BY) over pulling raw rows when the \
question asks for a total, ranking, or comparison.
- Add a LIMIT clause when a query could return many rows and only the top results matter.
- If a query errors, read the error message and fix the query rather than giving up.
- Once you have the data you need, give a concise, direct answer in plain English \
that cites the actual numbers returned. Do not fabricate data or speculate beyond \
what the query results show."""


def ask_question(question: str) -> dict:
    """Run the tool-use loop: Claude writes SQL, we execute it, Claude explains the result."""
    client = anthropic.Anthropic()
    messages: list[dict] = [{"role": "user", "content": question}]
    executed_queries: list[dict] = []
    last_successful_query: dict | None = None

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.messages.create(
            model=MODEL_NAME,
            max_tokens=1500,
            system=_system_prompt(),
            tools=[EXECUTE_SQL_TOOL],
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            answer = "".join(b.text for b in response.content if b.type == "text").strip()
            return {
                "answer": answer or "I wasn't able to produce an answer for that question.",
                "sql": last_successful_query["sql"] if last_successful_query else None,
                "columns": last_successful_query["columns"] if last_successful_query else [],
                "rows": last_successful_query["rows"] if last_successful_query else [],
                "row_count": last_successful_query["row_count"] if last_successful_query else 0,
                "queries": executed_queries,
            }

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type != "tool_use":
                continue
            sql = block.input.get("query", "")
            try:
                result = execute_query(sql)
                executed_queries.append({"sql": sql, "error": None})
                last_successful_query = {"sql": sql, **result}
                content = json.dumps(
                    {
                        "columns": result["columns"],
                        "rows": result["rows"],
                        "row_count": result["row_count"],
                        "truncated": result["truncated"],
                    },
                    default=str,
                )
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": content}
                )
            except QueryError as exc:
                executed_queries.append({"sql": sql, "error": str(exc)})
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Error: {exc}",
                        "is_error": True,
                    }
                )

        messages.append({"role": "user", "content": tool_results})

    return {
        "answer": "I couldn't settle on an answer within the allowed number of steps. Try rephrasing the question.",
        "sql": last_successful_query["sql"] if last_successful_query else None,
        "columns": last_successful_query["columns"] if last_successful_query else [],
        "rows": last_successful_query["rows"] if last_successful_query else [],
        "row_count": last_successful_query["row_count"] if last_successful_query else 0,
        "queries": executed_queries,
    }
