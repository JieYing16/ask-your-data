import json
import logging
import os
import time

from app.database import QueryError, execute_query, get_schema_ddl

logger = logging.getLogger("ask_your_data.llm")

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
MAX_TOOL_ITERATIONS = 4

# One-line description of the dataset, injected into the prompt so the model knows
# the domain. Override via DB_DESCRIPTION when pointing at a non-Chinook database.
DB_DESCRIPTION = os.environ.get(
    "DB_DESCRIPTION", 'the "Chinook" digital music store database'
).strip()

SQL_TOOL_NAME = "execute_sql"
SQL_TOOL_DESCRIPTION = (
    f"Run a single read-only SQL SELECT statement against {DB_DESCRIPTION} and "
    "return the resulting rows. Use this to explore the data and to compute the "
    "final answer. Only SELECT/WITH statements are permitted."
)
SQL_TOOL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "A single read-only SQL SELECT statement.",
        }
    },
    "required": ["query"],
}


def _system_prompt() -> str:
    schema = get_schema_ddl()
    return f"""You are a data analyst assistant for {DB_DESCRIPTION}.

You answer natural-language questions by writing and running SQL SELECT queries \
against the database via the execute_sql tool, then explaining the result in plain \
language.

Database schema (SQLite DDL):

{schema}

Rules:
- Always call execute_sql at least once before answering — never guess at numbers.
- Only SELECT/WITH statements are allowed; the tool rejects anything else.
- Table and column names are case-sensitive; use the exact names in the schema above.
- Prefer aggregate queries (COUNT, SUM, AVG, GROUP BY) over pulling raw rows when the \
question asks for a total, ranking, or comparison.
- Add a LIMIT clause when a query could return many rows and only the top results matter.
- If a query errors, read the error message and fix the query rather than giving up.
- Once you have the data you need, give a concise, direct answer in plain English \
that cites the actual numbers returned. Do not fabricate data or speculate beyond \
what the query results show."""


def _finalize(answer: str, executed_queries: list[dict], last_successful_query: dict | None) -> dict:
    return {
        "answer": answer,
        "sql": last_successful_query["sql"] if last_successful_query else None,
        "columns": last_successful_query["columns"] if last_successful_query else [],
        "rows": last_successful_query["rows"] if last_successful_query else [],
        "row_count": last_successful_query["row_count"] if last_successful_query else 0,
        "queries": executed_queries,
    }


def _run_sql_tool(sql: str) -> tuple[str, dict | None, bool]:
    """Execute a tool-called SQL query. Returns (content_for_model, executed_query_record, is_error)."""
    try:
        result = execute_query(sql)
        content = json.dumps(
            {
                "columns": result["columns"],
                "rows": result["rows"],
                "row_count": result["row_count"],
                "truncated": result["truncated"],
            },
            default=str,
        )
        return content, {"sql": sql, **result}, False
    except QueryError as exc:
        return f"Error: {exc}", None, True


def _ask_anthropic(question: str) -> dict:
    """Tool-use loop against the Anthropic API: Claude writes SQL, we execute it, Claude explains the result."""
    import anthropic

    model_name = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
    tool = {
        "name": SQL_TOOL_NAME,
        "description": SQL_TOOL_DESCRIPTION,
        "input_schema": SQL_TOOL_INPUT_SCHEMA,
    }

    client = anthropic.Anthropic()
    system_prompt = _system_prompt()
    messages: list[dict] = [{"role": "user", "content": question}]
    executed_queries: list[dict] = []
    last_successful_query: dict | None = None

    for i in range(MAX_TOOL_ITERATIONS):
        started = time.perf_counter()
        response = client.messages.create(
            model=model_name,
            max_tokens=1500,
            system=system_prompt,
            tools=[tool],
            messages=messages,
        )
        logger.info("anthropic call %d took %.2f s", i + 1, time.perf_counter() - started)

        if response.stop_reason != "tool_use":
            answer = "".join(b.text for b in response.content if b.type == "text").strip()
            return _finalize(
                answer or "I wasn't able to produce an answer for that question.",
                executed_queries,
                last_successful_query,
            )

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type != "tool_use":
                continue
            sql = block.input.get("query", "")
            content, record, is_error = _run_sql_tool(sql)
            executed_queries.append({"sql": sql, "error": None if not is_error else content})
            if record:
                last_successful_query = record
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                    **({"is_error": True} if is_error else {}),
                }
            )

        messages.append({"role": "user", "content": tool_results})

    return _finalize(
        "I couldn't settle on an answer within the allowed number of steps. Try rephrasing the question.",
        executed_queries,
        last_successful_query,
    )


def _ask_ollama(question: str) -> dict:
    """Tool-use loop against a local Ollama server — a free, no-API-key alternative to Claude."""
    import ollama

    model_name = os.environ.get("OLLAMA_MODEL", "llama3.1")
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    tool = {
        "type": "function",
        "function": {
            "name": SQL_TOOL_NAME,
            "description": SQL_TOOL_DESCRIPTION,
            "parameters": SQL_TOOL_INPUT_SCHEMA,
        },
    }

    client = ollama.Client(host=host)
    messages: list[dict] = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": question},
    ]
    executed_queries: list[dict] = []
    last_successful_query: dict | None = None

    for i in range(MAX_TOOL_ITERATIONS):
        started = time.perf_counter()
        response = client.chat(model=model_name, messages=messages, tools=[tool])
        logger.info("ollama call %d took %.2f s", i + 1, time.perf_counter() - started)
        message = response["message"]
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            answer = (message.get("content") or "").strip()
            return _finalize(
                answer or "I wasn't able to produce an answer for that question.",
                executed_queries,
                last_successful_query,
            )

        messages.append(message)

        for call in tool_calls:
            sql = call["function"]["arguments"].get("query", "")
            content, record, is_error = _run_sql_tool(sql)
            executed_queries.append({"sql": sql, "error": None if not is_error else content})
            if record:
                last_successful_query = record
            messages.append({"role": "tool", "content": content, "tool_name": SQL_TOOL_NAME})

    return _finalize(
        "I couldn't settle on an answer within the allowed number of steps. Try rephrasing the question.",
        executed_queries,
        last_successful_query,
    )


def ask_question(question: str) -> dict:
    """Run the tool-use loop against whichever LLM_PROVIDER is configured (anthropic or ollama)."""
    if LLM_PROVIDER == "ollama":
        return _ask_ollama(question)
    if LLM_PROVIDER == "anthropic":
        return _ask_anthropic(question)
    raise ValueError(f"Unknown LLM_PROVIDER '{LLM_PROVIDER}'. Use 'anthropic' or 'ollama'.")
