# Ask Your Data

![Python](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.139-009688?logo=fastapi&logoColor=white)
![DuckDB](https://img.shields.io/badge/DuckDB-1.5-FFF000?logo=duckdb&logoColor=black)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)
![Claude](https://img.shields.io/badge/LLM-Claude-D97757?logo=anthropic&logoColor=white)

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

## Quickstart

### Option A — Docker (recommended)

1. **Get an API key** from [console.anthropic.com](https://console.anthropic.com/) if you don't have one.
2. **Clone and enter the repo:**
   ```bash
   git clone https://github.com/JieYing16/ask-your-data.git
   cd ask-your-data
   ```
3. **Create your env file:**
   ```bash
   cp .env.example .env
   ```
4. **Add your key** — open `.env` and set:
   ```
   ANTHROPIC_API_KEY=sk-ant-your-key-here
   ```
5. **Build and start the container:**
   ```bash
   docker compose up --build
   ```
   First run takes a minute or two (installs dependencies, bakes the DuckDB SQLite extension into the image). Subsequent runs are fast.
6. **Open the app** at [http://localhost:8000](http://localhost:8000).
7. **Stop it** when you're done — `Ctrl+C`, then `docker compose down`.

### Option B — Run locally, without Docker

1. **Prerequisites:** Python 3.10+ and pip.
2. **Clone and enter the repo** (same as step 2 above).
3. **(Optional but recommended) create a virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate        # macOS/Linux
   venv\Scripts\activate           # Windows (cmd/PowerShell)
   ```
4. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
5. **Set your API key** for the current shell session:
   ```bash
   export ANTHROPIC_API_KEY=sk-ant-your-key-here     # macOS/Linux/Git Bash
   ```
   ```powershell
   $env:ANTHROPIC_API_KEY = "sk-ant-your-key-here"   # PowerShell
   ```
6. **Start the server:**
   ```bash
   uvicorn app.main:app --reload
   ```
7. **Open the app** at [http://localhost:8000](http://localhost:8000).
8. **Stop it** with `Ctrl+C`.

> Optional: set `ANTHROPIC_MODEL` (defaults to `claude-opus-4-8`) to try a cheaper/faster model, e.g. `claude-haiku-4-5`.

## Optional: Auto-PR hook with Ollama

The auto-PR hook can use a local Ollama instance for free code reviews and PR description generation.

### Installation

1. **Install Ollama**: Download from https://ollama.ai
2. **Start the Ollama server**:
   ```bash
   ollama serve
   ```
3. **Pull a model** (if not already present):
   ```bash
   ollama pull qwen2.5-coder:7b  # Default model
   # Or use a larger model if you have the resources:
   ollama pull mistral:latest
   ```

### Configuration

Set environment variables to customize the hook behavior:

```powershell
# Use a different model
$env:OLLAMA_MODEL = "mistral:latest"

# Connect to a remote Ollama instance
$env:OLLAMA_HOST = "http://192.168.1.100:11434"

# Enable debug logging
$env:CLAUDE_AUTO_PR_DEBUG = "true"
```

### Fallback options

If Ollama is unavailable, the hook will:
- Generate a generic PR description
- Skip code review entirely
- Log warnings about what was skipped

To enable OpenAI as a fallback (paid):

```powershell
$env:OPENAI_API_KEY = "sk-..."
```

### Troubleshooting

- **"Ollama server not accessible"**: Verify `ollama serve` is running
- **"Model not found"**: Run `ollama pull <model-name>`
- **Slow code review**: Use a smaller model like `qwen2.5-coder:7b` instead of larger ones
- **Out of memory**: Reduce the model size or increase available VRAM

## Walkthrough

1. **Load the page.** You'll see a row of pills listing the 11 available tables (Customer, Invoice, Track, Artist, ...) — that's the schema Claude has access to — plus a row of clickable example questions.
2. **Ask a question.** Type something into the box (or click an example chip) and hit **Ask** — e.g. *"Which 5 genres generate the most revenue?"*
3. **Claude works in the background.** It writes a SQL query, the app runs it against the database, and Claude reads the real result back before answering — you'll see a brief "Thinking…" state while this happens.
4. **Read the answer.** A plain-English answer appears first, citing the actual numbers returned.
5. **Check the SQL.** Below the answer, an expandable **SQL query** panel shows exactly what ran — this is what makes the answer verifiable rather than a black box.
6. **Check the data.** Below that, a results table shows the raw rows the answer was based on (capped at 200 rows, with a note if more were truncated).
7. **Ask a follow-up.** Try a different question — each request is independent, so you can jump between topics freely.

Good questions to try (these force joins/aggregations, which shows off the SQL generation better than a flat lookup):
- "Which 5 genres generate the most revenue?"
- "Who are the top 10 customers by total spend?"
- "What's the average invoice total by country?"
- "Which employee has the most customers assigned to them?"
- "What are the 10 longest tracks in the catalog?"

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
