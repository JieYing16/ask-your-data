import json
import os

import anthropic
import ollama

from app.database import QueryError, execute_query, get_schema_ddl

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()

MODEL_NAME = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")

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

OLLAMA_TOOL = {
    "type": "function",
    "function": {
        "name": EXECUTE_SQL_TOOL["name"],
        "description": EXECUTE_SQL_TOOL["description"],
        "parameters": EXECUTE_SQL_TOOL["input_schema"],
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


def _run_sql_tool(sql: str, executed_queries: list[dict]) -> tuple[str, dict | None]:
    """Execute a single SQL tool call and return (result content, last successful query)."""
    try:
        result = execute_query(sql)
        executed_queries.append({"sql": sql, "error": None})
        content = json.dumps(
            {
                "columns": result["columns"],
                "rows": result["rows"],
                "row_count": result["row_count"],
                "truncated": result["truncated"],
            },
            default=str,
        )
        return content, {"sql": sql, **result}
    except QueryError as exc:
        executed_queries.append({"sql": sql, "error": str(exc)})
        return f"Error: {exc}", None


def _build_response(answer: str, last_successful_query: dict | None, executed_queries: list[dict]) -> dict:
    return {
        "answer": answer,
        "sql": last_successful_query["sql"] if last_successful_query else None,
        "columns": last_successful_query["columns"] if last_successful_query else [],
        "rows": last_successful_query["rows"] if last_successful_query else [],
        "row_count": last_successful_query["row_count"] if last_successful_query else 0,
        "queries": executed_queries,
    }


def ask_question(question: str) -> dict:
    """Run the tool-use loop: the model writes SQL, we execute it, the model explains the result."""
    if LLM_PROVIDER == "ollama":
        return _ask_ollama(question)
    return _ask_anthropic(question)


def _ask_anthropic(question: str) -> dict:
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
            return _build_response(
                answer or "I wasn't able to produce an answer for that question.",
                last_successful_query,
                executed_queries,
            )

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type != "tool_use":
                continue
            sql = block.input.get("query", "")
            content, successful = _run_sql_tool(sql, executed_queries)
            if successful is not None:
                last_successful_query = successful
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                    **({"is_error": True} if successful is None else {}),
                }
            )

        messages.append({"role": "user", "content": tool_results})

    return _build_response(
        "I couldn't settle on an answer within the allowed number of steps. Try rephrasing the question.",
        last_successful_query,
        executed_queries,
    )


def _ask_ollama(question: str) -> dict:
    client = ollama.Client(host=OLLAMA_HOST)
    messages: list[dict] = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": question},
    ]
    executed_queries: list[dict] = []
    last_successful_query: dict | None = None

    for _ in range(MAX_TOOL_ITERATIONS):
        try:
            response = client.chat(model=OLLAMA_MODEL, messages=messages, tools=[OLLAMA_TOOL])
        except ConnectionError as exc:
            raise RuntimeError(
                f"Could not connect to Ollama at {OLLAMA_HOST}. Is `ollama serve` running?"
            ) from exc
        except ollama.ResponseError as exc:
            raise RuntimeError(f"Ollama returned an error for model '{OLLAMA_MODEL}': {exc.error}") from exc

        message = response.message
        tool_calls = message.tool_calls or []

        if not tool_calls:
            answer = (message.content or "").strip()
            return _build_response(
                answer or "I wasn't able to produce an answer for that question.",
                last_successful_query,
                executed_queries,
            )

        messages.append(message.model_dump())

        for call in tool_calls:
            sql = (call.function.arguments or {}).get("query", "")
            content, successful = _run_sql_tool(sql, executed_queries)
            if successful is not None:
                last_successful_query = successful
            messages.append({"role": "tool", "content": content, "name": call.function.name})

    return _build_response(
        "I couldn't settle on an answer within the allowed number of steps. Try rephrasing the question.",
        last_successful_query,
        executed_queries,
    )
