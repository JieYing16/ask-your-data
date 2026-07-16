# Ask Your Data

A natural-language interface over a real relational database. Ask a question in plain English, Claude writes and runs the SQL, and you get back a plain-language answer alongside the exact query and result set it used.

```
"Which 5 genres generate the most revenue?"
        │
        ▼
Claude (tool use) ──► execute_sql("SELECT g.Name, SUM(...) ...")
        │                                │
        │                     DuckDB ──► Chinook.db (SQLite)
        ▼                                │
"Rock leads by a wide margin at $826..." ◄┘
```

## Stack

- **LLM**: Claude (Anthropic API), driving a tool-use loop — it writes SQL, we execute it, it reads the results back and explains them.
- **Database**: [Chinook](https://github.com/lerocha/chinook-database) — a real, well-known relational dataset (11 tables: customers, invoices, tracks, artists, employees, etc.), queried through **DuckDB**'s SQLite scanner.
- **Backend**: FastAPI (`/api/ask`, `/api/schema`, `/api/health`).
- **Frontend**: a single static HTML page — no build step.
- **Containerized**: Dockerfile + docker-compose.

## Why this dataset/approach

The point of this project is to demonstrate an LLM-powered "ask your data" pattern against **real SQL and real relational structure** — not a single flat table. Claude is given the actual `CREATE TABLE` DDL (including foreign keys) as its schema context, and every answer is grounded in a query it actually ran; nothing is fabricated. The generated SQL is always shown so the answer is auditable.

## Running it

### Docker (recommended)

```bash
cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY

docker compose up --build
```

Open http://localhost:8000

### Locally, without Docker

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn app.main:app --reload
```

## How it works

1. The full database schema (DDL, with primary/foreign keys) is embedded in the system prompt.
2. Claude is given one tool, `execute_sql`, and told to use it before answering.
3. Each SQL statement is validated server-side before it runs: only a single read-only `SELECT`/`WITH` statement is allowed — no `INSERT`/`UPDATE`/`DELETE`/`DROP`/`ATTACH`/etc. The DuckDB connection also attaches the underlying SQLite file in read-only mode as a second layer of defense.
4. Claude can call the tool multiple times to explore before committing to a final answer.
5. The API returns the natural-language answer, the SQL that produced it, and the result rows — the frontend renders all three.

## Project structure

```
app/
  database.py   # DuckDB connection, schema introspection, query validation/execution
  llm.py        # system prompt + Claude tool-use loop
  main.py       # FastAPI routes
static/
  index.html    # frontend (vanilla JS, no build step)
data/
  chinook.db    # sample dataset (SQLite)
```

## Try it

- "Which 5 genres generate the most revenue?"
- "Who are the top 10 customers by total spend?"
- "What's the average invoice total by country?"
- "Which employee has the most customers assigned to them?"
- "What are the 10 longest tracks in the catalog?"
